"""Prospect watch service: the tracked set (``linkedin_profiles.tracked``), who is due, the
per-profile watch state, and starting a watch run.

Phase 2 (CRM) will set ``tracked`` itself when it creates a prospect and subscribes to
``events.TrackedProfileHit``; nothing here depends on prospect/lead tables.

Watch state lives in columns on ``linkedin_profiles`` (``last_checked_at``, ``watch_checks``,
``watch_hits``, ``watch_last_status``, ``watch_disabled_until``). A watch RUN is a normal
``scrape_runs`` row (so overlap, the safety cool-down and the daily post cap are the existing
ones) marked by a ``watch_runs`` row; its per-profile log rows are ``scrape_run_keywords`` rows
with ``keyword_text`` = ``watch:<slug>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, delete, or_, select, update
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc, utcnow
from sales0.config import get_settings
from sales0.db.models import LinkedinProfile, ScrapeRun, ScrapeRunKeyword, StatusHistory, WatchRun
from sales0.errors import DomainError
from sales0.jobs.runner import get_runner
from sales0.services import runs as runs_service
from sales0.services._common import not_found, now_or, record_history
from sales0.services.views import RunView

WATCH_PREFIX = "watch:"
PAUSED_FOREVER = datetime(2100, 1, 1)  # watch_disabled_until for "paused by the user"
STATUSES = ("ok", "no_posts", "drift", "error", "stopped", "skipped")


@dataclass(frozen=True)
class TrackedView:
    id: int
    slug: str
    kind: str
    profile_url: str
    display_name: str | None
    headline: str | None
    last_checked_at: datetime | None
    next_due_at: datetime | None
    watch_checks: int
    watch_hits: int
    watch_last_status: str | None
    paused: bool
    paused_until: datetime | None
    posts_seen: int
    relevant_count: int


def _paused(row: LinkedinProfile, now: datetime) -> bool:
    return row.watch_disabled_until is not None and ensure_utc(row.watch_disabled_until) > now


def _view(row: LinkedinProfile, now: datetime) -> TrackedView:
    gap = timedelta(days=get_settings().watch_min_days_between_checks)
    due = ensure_utc(row.last_checked_at) + gap if row.last_checked_at else None
    paused = _paused(row, now)
    return TrackedView(
        id=row.id, slug=row.slug, kind=row.kind, profile_url=row.profile_url, display_name=row.display_name,
        headline=row.headline, last_checked_at=row.last_checked_at, next_due_at=due,
        watch_checks=row.watch_checks or 0, watch_hits=row.watch_hits or 0, watch_last_status=row.watch_last_status,
        paused=paused, paused_until=row.watch_disabled_until if paused else None,
        posts_seen=row.posts_seen or 0, relevant_count=row.relevant_count or 0,
    )  # fmt: skip


def _get(db: Session, profile_id: int) -> LinkedinProfile:
    row = db.get(LinkedinProfile, profile_id)
    if row is None:
        raise not_found()
    return row


# ---------------------------------------------------------------------- tracked set
def list_tracked(db: Session, *, now: datetime | None = None, include_untracked_ids: tuple[int, ...] = ()) -> list[TrackedView]:
    stamp = now_or(now)
    rows = db.scalars(
        select(LinkedinProfile)
        .where(LinkedinProfile.tracked.is_(True))
        .order_by(
            case((LinkedinProfile.last_checked_at.is_(None), 0), else_=1),
            LinkedinProfile.last_checked_at,
            LinkedinProfile.id,
        )
    ).all()
    return [_view(r, stamp) for r in rows]


def is_tracked(db: Session, profile_id: int | None) -> bool:
    if profile_id is None:
        return False
    return bool(db.scalar(select(LinkedinProfile.tracked).where(LinkedinProfile.id == profile_id)))


def _set_via_crm(db: Session, row: LinkedinProfile, tracked: bool, actor_user_id: int | None) -> None:
    """Set the flag through the CRM when a prospect owns the profile (timeline 'tracked_changed'); else directly."""
    from sales0.db.models import Prospect
    from sales0.services.crm import prospects

    pid = db.scalar(select(Prospect.id).where(Prospect.linkedin_profile_id == row.id))
    if pid is not None:
        prospects.set_tracked(db, pid, tracked)
    row.tracked = tracked


def track(db: Session, profile_id: int, *, actor_user_id: int | None = None) -> TrackedView:
    row = _get(db, profile_id)
    row.watch_disabled_until = None
    _set_via_crm(db, row, True, actor_user_id)
    db.flush()
    return _view(row, utcnow())


def untrack(db: Session, profile_id: int, *, actor_user_id: int | None = None) -> TrackedView:
    """Stop watching. ``tracked`` becomes False (not NULL), so it is clear it was a decision."""
    row = _get(db, profile_id)
    _set_via_crm(db, row, False, actor_user_id)
    db.flush()
    return _view(row, utcnow())


def set_paused(db: Session, profile_id: int, paused: bool) -> TrackedView:
    row = _get(db, profile_id)
    row.watch_disabled_until = PAUSED_FOREVER if paused else None
    db.flush()
    return _view(row, utcnow())


def track_author_of_enquiry(db: Session, enquiry_id: int, *, actor_user_id: int | None = None) -> TrackedView:
    """The inbox 'Track' button: track the enquiry's author profile."""
    from sales0.db.models import Enquiry

    pid = db.scalar(select(Enquiry.author_profile_id).where(Enquiry.id == enquiry_id))
    if pid is None:
        raise DomainError("no_profile", "This enquiry has no author profile link to track.", 409)
    return track(db, pid, actor_user_id=actor_user_id)


# ---------------------------------------------------------------------- planning
def due_profiles(db: Session, limit: int | None = None, *, now: datetime | None = None) -> list[LinkedinProfile]:
    """Tracked, not paused, last checked at least ``watch_min_days_between_checks`` ago (or never),
    least-recently-checked first (never-checked first). Untracked profiles are never returned."""
    settings = get_settings()
    stamp = now_or(now)
    cutoff = stamp - timedelta(days=settings.watch_min_days_between_checks)
    n = settings.watch_max_profiles_per_run if limit is None else limit
    return list(
        db.scalars(
            select(LinkedinProfile)
            .where(
                LinkedinProfile.tracked.is_(True),
                or_(LinkedinProfile.watch_disabled_until.is_(None), LinkedinProfile.watch_disabled_until <= stamp),
                or_(LinkedinProfile.last_checked_at.is_(None), LinkedinProfile.last_checked_at <= cutoff),
            )
            .order_by(
                case((LinkedinProfile.last_checked_at.is_(None), 0), else_=1),
                LinkedinProfile.last_checked_at,
                LinkedinProfile.id,
            )
            .limit(max(n, 0))
        )
    )


def due_count(db: Session, *, now: datetime | None = None) -> int:
    return len(due_profiles(db, limit=10_000, now=now))


def record_check(db: Session, profile_id: int, status: str, *, hits: int = 0, now: datetime | None = None) -> None:
    """Stamp a visit (only visits that reached the page count as a check; a stop does not)."""
    values = {"watch_last_status": status}
    if status != "stopped":
        values["last_checked_at"] = now_or(now)
    db.execute(
        update(LinkedinProfile)
        .where(LinkedinProfile.id == profile_id)
        .values(
            **values,
            watch_checks=LinkedinProfile.watch_checks + (0 if status == "stopped" else 1),
            watch_hits=LinkedinProfile.watch_hits + hits,
        )
    )
    db.flush()
    db.expire_all()


# ---------------------------------------------------------------------- runs
def is_watch_run(db: Session, run_id: int) -> bool:
    return db.get(WatchRun, run_id) is not None


def watch_eligibility(db: Session, now: datetime | None = None) -> tuple[DomainError | None, int]:
    """``(why a watch run cannot start now | None, profiles due)``."""
    stamp = now_or(now)
    error, _, _ = runs_service._check(db, stamp, watch=True)
    due = due_count(db, now=stamp)
    if error is None and due == 0:
        error = DomainError("nothing_due", "No tracked profile is due for a check.", 409)
    return error, due


def start_watch_run(db: Session, origin: str = "manual", actor_user_id: int | None = None) -> RunView:
    """Queue a watch run and hand it to the job runner (the one browser slot, same as discovery)."""
    from sales0.jobs import watch_job

    now = utcnow()
    error, _ = watch_eligibility(db, now)
    if error is not None:
        raise error
    run = ScrapeRun(
        trigger="manual", mode=get_settings().linkedin_mode, status="queued", started_at=now,
        current_step="Queued - starting the browser", actor_user_id=actor_user_id,
    )
    db.add(run)
    db.flush()
    db.add(WatchRun(run_id=run.id, origin=origin))
    record_history(db, "scrape_run", run.id, "status", None, "queued", actor_user_id=actor_user_id, now=now)
    db.commit()
    run_id = run.id
    try:
        get_runner().submit("run", lambda: watch_job.execute(run_id))
    except DomainError:
        db.execute(delete(StatusHistory).where(StatusHistory.entity_type == "scrape_run", StatusHistory.entity_id == run_id))
        db.delete(run)  # watch_runs cascades
        db.commit()
        raise
    return runs_service.run_view(run)


@dataclass(frozen=True)
class WatchLogRow:
    slug: str
    status: str
    posts_seen: int
    posts_new: int
    posts_relevant: int
    errors: int
    message: str | None
    cost_usd_micros: int


def run_log(db: Session, run_id: int) -> list[WatchLogRow]:
    rows = db.scalars(
        select(ScrapeRunKeyword).where(ScrapeRunKeyword.run_id == run_id).order_by(ScrapeRunKeyword.id)
    ).all()
    return [
        WatchLogRow(
            slug=r.keyword_text.removeprefix(WATCH_PREFIX), status=r.status, posts_seen=r.posts_seen, posts_new=r.posts_new,
            posts_relevant=r.posts_relevant, errors=r.errors, message=r.error_message, cost_usd_micros=r.cost_usd_micros,
        )
        for r in rows
        if r.keyword_text.startswith(WATCH_PREFIX)
    ]


def latest_watch_run(db: Session) -> RunView | None:
    run = db.scalar(
        select(ScrapeRun).join(WatchRun, WatchRun.run_id == ScrapeRun.id).order_by(ScrapeRun.id.desc()).limit(1)
    )
    return runs_service.run_view(run) if run is not None else None


def hits_since(db: Session, since: datetime, until: datetime) -> list[tuple[str, int, int]]:
    """``(profile name/slug, enquiry_id, profile_id)`` for enquiries a watch run found in the window."""
    from sales0.db.models import Enquiry, EnquiryKeywordHit

    rows = db.execute(
        select(LinkedinProfile.display_name, LinkedinProfile.slug, Enquiry.id, LinkedinProfile.id)
        .join(Enquiry, Enquiry.author_profile_id == LinkedinProfile.id)
        .join(EnquiryKeywordHit, EnquiryKeywordHit.enquiry_id == Enquiry.id)
        .where(
            EnquiryKeywordHit.keyword_text.like(WATCH_PREFIX + "%"),
            EnquiryKeywordHit.seen_at >= since,
            EnquiryKeywordHit.seen_at < until,
        )
        .order_by(Enquiry.id)
    ).all()
    seen: set[int] = set()
    out: list[tuple[str, int, int]] = []
    for name, slug, eid, pid in rows:
        if eid not in seen:
            seen.add(eid)
            out.append((name or slug, int(eid), int(pid)))
    return out


__all__ = [
    "TrackedView", "WatchLogRow", "due_count", "due_profiles", "hits_since", "is_tracked", "is_watch_run",
    "latest_watch_run", "list_tracked", "record_check", "run_log", "set_paused", "start_watch_run", "track",
    "track_author_of_enquiry", "untrack", "watch_eligibility",
]
