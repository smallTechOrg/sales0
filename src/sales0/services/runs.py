"""Discovery runs: eligibility, start (queue + submit to the runner), read models, recovery."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc, local_day_bounds_utc, local_now, utcnow
from sales0.config import get_settings
from sales0.db.engine import session_scope
from sales0.db.models import Keyword, LlmCall, ScrapeRun, ScrapeRunKeyword, StatusHistory
from sales0.errors import DomainError
from sales0.jobs.runner import get_runner, job_busy_error
from sales0.logging import get_logger
from sales0.services._common import not_found, now_or, record_history
from sales0.services.session import get_session_row
from sales0.services.views import Eligibility, RunDetailView, RunKeywordView, RunView

if TYPE_CHECKING:
    from sales0.scraper.types import KeywordScrapeResult

log = get_logger("sales0.services.runs")

ACTIVE_STATUSES = ("queued", "running")
INTERRUPTED_MESSAGE = "Interrupted by server restart"

# Safety-stop kinds -> which cool-down setting applies (data flow step 6c).
LONG_COOLDOWN_KINDS = ("rate_limited", "captcha")  # SALES0_SAFETY_COOLDOWN_MIN
SHORT_COOLDOWN_KINDS = ("checkpoint", "logout")  # SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN
COOLDOWN_REASON_PREFIX = "LinkedIn asked us to slow down — next run allowed after "

# keyword_outcome: field_misses keys in the order the message lists them (data flow step 3).
FIELD_LABELS: dict[str, str] = {
    "text": "post text",  # Phase 1.2: containers with a ref but no readable text (listed first)
    "author_name": "author",
    "author_headline": "headline",
    "posted_relative": "post time",
    "author_profile_url": "profile link",
}
LAYOUT_SUFFIX = "LinkedIn layout may have changed"


# ---------------------------------------------------------------------- keyword outcome
def _cards_unreadable(n: int) -> str:
    return f"{n} card{'' if n == 1 else 's'} couldn't be read"


def keyword_outcome(result: KeywordScrapeResult) -> tuple[Literal["done", "warning"], str | None]:
    """Keyword status + Message for a finished search (pure; architecture.md data flow step 3).

    1. ``drift_warning`` -> warning "No posts parsed — … (N result cards found)".
    2. Any field missing on >= half of the parsed posts (and >= 2 posts) -> warning
       "Parsed N posts, but couldn't read <field> on M — …".
    3. Otherwise done; smaller misses and unreadable cards are still written down.
    """
    n_posts = len(result.posts)
    parse_errors = int(result.parse_errors or 0)
    raw_misses = {
        name: int(count)
        for name, count in (getattr(result, "field_misses", None) or {}).items()
        if name in FIELD_LABELS and int(count) > 0
    }
    # "text" misses are containers that yielded no post, so their denominator is
    # posts + text misses; every other field is counted over the parsed posts.
    totals = {name: (n_posts + raw_misses[name] if name == "text" else n_posts) for name in raw_misses}
    misses = {name: min(count, totals[name]) for name, count in raw_misses.items()}
    ordered = [(name, misses[name]) for name in FIELD_LABELS if name in misses]

    if result.drift_warning:
        cards = int(getattr(result, "cards_found", 0) or 0)
        noun = "result card" if cards == 1 else "result cards"
        return "warning", f"No posts parsed — {LAYOUT_SUFFIX} ({cards} {noun} found)"

    heavy = [(name, m) for name, m in ordered if totals[name] >= 2 and m * 2 >= totals[name]]
    if heavy:
        listed = ", ".join(f"{FIELD_LABELS[name]} on {m}" for name, m in heavy)
        message = f"Parsed {n_posts} posts, but couldn't read {listed} — {LAYOUT_SUFFIX}"
        if parse_errors:
            message = f"{message} · {_cards_unreadable(parse_errors)}"
        return "warning", message

    parts: list[str] = []
    if parse_errors:
        parts.append(_cards_unreadable(parse_errors))
    parts.extend(f"{FIELD_LABELS[name]} missing on {m}/{totals[name]}" for name, m in ordered)
    return "done", (" · ".join(parts) if parts else None)


# ---------------------------------------------------------------------- read models
def _duration_ms(run: ScrapeRun, now: datetime | None = None) -> int | None:
    if run.started_at is None:
        return None
    end = run.finished_at or (now_or(now) if run.status in ACTIVE_STATUSES else None)
    if end is None:
        return None
    return max(int((ensure_utc(end) - ensure_utc(run.started_at)).total_seconds() * 1000), 0)


def _run_costs(db: Session, run_ids: list[int]) -> dict[int, int]:
    if not run_ids:
        return {}
    rows = db.execute(
        select(LlmCall.run_id, func.coalesce(func.sum(LlmCall.cost_usd_micros), 0))
        .where(LlmCall.run_id.in_(run_ids))
        .group_by(LlmCall.run_id)
    ).all()
    return {int(rid): int(total) for rid, total in rows}


def _layout_warnings(db: Session, run_ids: list[int]) -> dict[int, int]:
    """Per run: the number of keyword rows with status ``warning`` (computed on read)."""
    if not run_ids:
        return {}
    rows = db.execute(
        select(ScrapeRunKeyword.run_id, func.count(ScrapeRunKeyword.id))
        .where(ScrapeRunKeyword.run_id.in_(run_ids), ScrapeRunKeyword.status == "warning")
        .group_by(ScrapeRunKeyword.run_id)
    ).all()
    return {int(rid): int(n) for rid, n in rows}


def run_view(run: ScrapeRun, cost_usd_micros: int = 0, layout_warnings: int = 0) -> RunView:
    return RunView(
        id=run.id,
        status=run.status,  # type: ignore[arg-type]
        trigger=run.trigger,
        mode=run.mode,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=_duration_ms(run),
        keywords_planned=run.keywords_planned,
        keywords_done=run.keywords_done,
        current_keyword=run.current_keyword,
        current_step=run.current_step,
        posts_seen=run.posts_seen,
        posts_new=run.posts_new,
        posts_dupe=run.posts_dupe,
        enquiries_triaged=run.enquiries_triaged,
        triage_failed=run.triage_failed,
        errors_count=run.errors_count,
        abort_reason=run.abort_reason,
        error_message=run.error_message,
        cost_usd_micros=cost_usd_micros,
        layout_warnings=layout_warnings,
    )


def _keyword_view(row: ScrapeRunKeyword) -> RunKeywordView:
    return RunKeywordView(
        keyword_text=row.keyword_text,
        status=row.status,
        posts_seen=row.posts_seen,
        posts_new=row.posts_new,
        posts_dupe=row.posts_dupe,
        errors=row.errors,
        duration_ms=row.duration_ms,
        error_message=row.error_message,
    )


def get_run(db: Session, run_id: int) -> RunDetailView:
    run = db.get(ScrapeRun, run_id)
    if run is None:
        raise not_found()
    keywords = db.scalars(
        select(ScrapeRunKeyword).where(ScrapeRunKeyword.run_id == run_id).order_by(ScrapeRunKeyword.id)
    ).all()
    cost = _run_costs(db, [run_id]).get(run_id, 0)
    warnings = sum(1 for k in keywords if k.status == "warning")
    return RunDetailView(run=run_view(run, cost, warnings), keywords=[_keyword_view(k) for k in keywords])


def list_runs(db: Session, limit: int = 20) -> list[RunView]:
    runs = db.scalars(select(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(max(int(limit), 1))).all()
    ids = [r.id for r in runs]
    costs = _run_costs(db, ids)
    warnings = _layout_warnings(db, ids)
    return [run_view(r, costs.get(r.id, 0), warnings.get(r.id, 0)) for r in runs]


def active_run(db: Session) -> RunView | None:
    run = db.scalar(
        select(ScrapeRun).where(ScrapeRun.status.in_(ACTIVE_STATUSES)).order_by(ScrapeRun.id.desc()).limit(1)
    )
    if run is None:
        return None
    return run_view(run, _run_costs(db, [run.id]).get(run.id, 0), _layout_warnings(db, [run.id]).get(run.id, 0))


# ---------------------------------------------------------------------- eligibility
def _day_counts(db: Session, now: datetime) -> tuple[int, int]:
    start, end = local_day_bounds_utc(get_settings().tz, now)
    runs_today, posts_today = db.execute(
        select(func.count(ScrapeRun.id), func.coalesce(func.sum(ScrapeRun.posts_seen), 0)).where(
            ScrapeRun.started_at >= start, ScrapeRun.started_at < end
        )
    ).one()
    return int(runs_today or 0), int(posts_today or 0)


def posts_seen_today(db: Session, now: datetime | None = None) -> int:
    return _day_counts(db, now_or(now))[1]


def _active_keyword_count(db: Session) -> int:
    return int(db.scalar(select(func.count(Keyword.id)).where(Keyword.active.is_(True))) or 0)


def safety_cooldown_until(db: Session, now: datetime | None = None) -> datetime | None:
    """When Run now is allowed again after the latest LinkedIn safety stop (UTC), or None.

    Reads the latest ``aborted`` run (by ``finished_at``) whose ``abort_reason`` is a safety
    kind. rate_limited / captcha -> ``SALES0_SAFETY_COOLDOWN_MIN``; checkpoint / logout ->
    ``SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN``. None when that instant is not in the future
    or the applicable setting is 0. Derived from stored columns only (no migration).
    """
    now = ensure_utc(now_or(now))
    settings = get_settings()
    row = db.execute(
        select(ScrapeRun.abort_reason, ScrapeRun.finished_at)
        .where(
            ScrapeRun.status == "aborted",
            ScrapeRun.abort_reason.in_(LONG_COOLDOWN_KINDS + SHORT_COOLDOWN_KINDS),
            ScrapeRun.finished_at.is_not(None),
        )
        .order_by(ScrapeRun.finished_at.desc(), ScrapeRun.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    kind, finished_at = row
    minutes = (
        settings.safety_cooldown_min if kind in LONG_COOLDOWN_KINDS else settings.safety_cooldown_checkpoint_min
    )
    if minutes <= 0:
        return None
    until = ensure_utc(finished_at) + timedelta(minutes=minutes)
    return until if until > now else None


def cooldown_clock(until: datetime, now: datetime | None = None, tz: str | ZoneInfo | None = None) -> str:
    """``HH:MM`` (24-hour, local), with " tomorrow" when it falls on a later local day.

    Rounded UP to the minute, so "allowed after HH:MM" is never earlier than the truth.
    """
    zone = tz if tz is not None else get_settings().tz
    local = local_now(zone, ensure_utc(until))
    if local.second or local.microsecond:
        local = local.replace(second=0, microsecond=0) + timedelta(minutes=1)
    today = local_now(zone, ensure_utc(now_or(now))).date()
    text = local.strftime("%H:%M")
    return f"{text} tomorrow" if local.date() > today else text


def cooldown_reason(until: datetime, now: datetime | None = None) -> str:
    return f"{COOLDOWN_REASON_PREFIX}{cooldown_clock(until, now)}"


def _check(db: Session, now: datetime) -> tuple[DomainError | None, int, int]:
    """The first reason a run cannot start (api.md order), plus today's counts."""
    settings = get_settings()
    runs_today, posts_today = _day_counts(db, now)
    error: DomainError | None = None
    session = get_session_row(db)
    busy = get_runner().busy()
    has_active = db.scalar(select(ScrapeRun.id).where(ScrapeRun.status.in_(ACTIVE_STATUSES)).limit(1)) is not None
    if has_active or busy == "run":
        error = DomainError("run_in_progress", "A run is already in progress.", 409)
    elif session.status == "needs_attention":
        detail = (session.last_error or "resolve the LinkedIn warning").rstrip(". ")
        error = DomainError(
            "needs_attention",
            f"LinkedIn needs attention: {detail}. Press Connect LinkedIn in Setup — the sales0 Chrome window "
            "opens on LinkedIn so you can resolve it.",
            409,
        )
    elif (cooldown := safety_cooldown_until(db, now)) is not None:
        error = DomainError("safety_cooldown", cooldown_reason(cooldown, now), 429)
    elif session.status != "connected":
        error = DomainError("not_connected", "Connect LinkedIn first (Setup → Connect LinkedIn).", 409)
    elif busy is not None:
        error = job_busy_error(busy)
    elif _active_keyword_count(db) == 0:
        error = DomainError("no_keywords", "Add at least one active keyword in Setup.", 409)
    elif runs_today >= settings.scrape_max_runs_per_day:
        error = DomainError(
            "daily_run_cap",
            f"Daily run limit reached ({runs_today}/{settings.scrape_max_runs_per_day}). "
            f"Runs reset at midnight ({settings.timezone}).",
            429,
        )
    elif posts_today >= settings.scrape_max_posts_per_day:
        error = DomainError(
            "daily_post_cap",
            f"Daily post limit reached ({posts_today}/{settings.scrape_max_posts_per_day}) "
            "— protects your LinkedIn account.",
            429,
        )
    return error, runs_today, posts_today


def run_eligibility(db: Session, now: datetime | None = None) -> Eligibility:
    settings = get_settings()
    error, runs_today, posts_today = _check(db, now_or(now))
    return Eligibility(
        can_run=error is None,
        reason=error.message if error else None,
        runs_today=runs_today,
        runs_cap=settings.scrape_max_runs_per_day,
        posts_today=posts_today,
        posts_cap=settings.scrape_max_posts_per_day,
        reason_code=error.code if error else None,
    )


# ---------------------------------------------------------------------- start / recover
def start_run(db: Session, actor_user_id: int | None = None) -> RunView:
    """Queue a run and hand it to the job runner. Commits so the job thread can see the row."""
    from sales0.jobs import discovery_job

    now = utcnow()
    error, _, _ = _check(db, now)
    if error is not None:
        raise error
    settings = get_settings()
    run = ScrapeRun(
        trigger="manual",
        mode=settings.linkedin_mode,
        status="queued",
        started_at=now,
        current_step="Queued — starting the browser",
        actor_user_id=actor_user_id,
    )
    db.add(run)
    db.flush()
    record_history(db, "scrape_run", run.id, "status", None, "queued", actor_user_id=actor_user_id, now=now)
    db.commit()
    run_id = run.id
    try:
        get_runner().submit("run", lambda: discovery_job.execute(run_id))
    except DomainError:
        # Lost a race with another job: drop the queued row so it does not count as a run.
        db.execute(
            delete(StatusHistory).where(StatusHistory.entity_type == "scrape_run", StatusHistory.entity_id == run_id)
        )
        db.delete(run)
        db.commit()
        raise
    return run_view(run)


def recover_interrupted_runs(db: Session) -> int:
    """Mark runs left queued/running by a previous process as failed. Returns the count."""
    now = utcnow()
    runs = db.scalars(select(ScrapeRun).where(ScrapeRun.status.in_(ACTIVE_STATUSES))).all()
    for run in runs:
        record_history(db, "scrape_run", run.id, "status", run.status, "failed", note=INTERRUPTED_MESSAGE, now=now)
        run.status = "failed"
        run.error_message = INTERRUPTED_MESSAGE
        run.finished_at = now
        run.current_step = INTERRUPTED_MESSAGE
    if runs:
        ids = [r.id for r in runs]
        db.execute(
            update(ScrapeRunKeyword)
            .where(ScrapeRunKeyword.run_id.in_(ids), ScrapeRunKeyword.status.in_(("pending", "running")))
            .values(status="skipped", error_message=INTERRUPTED_MESSAGE)
        )
    db.flush()
    return len(runs)


def recover_interrupted_runs_on_startup() -> int:
    """Startup hook (web/app.py lifespan): own session, committed, logged."""
    with session_scope() as db:
        count = recover_interrupted_runs(db)
    if count:
        log.warning("runs_recovered", count=count, reason=INTERRUPTED_MESSAGE)
    return count


__all__ = [
    "active_run",
    "cooldown_clock",
    "cooldown_reason",
    "get_run",
    "keyword_outcome",
    "list_runs",
    "posts_seen_today",
    "recover_interrupted_runs",
    "recover_interrupted_runs_on_startup",
    "run_eligibility",
    "run_view",
    "safety_cooldown_until",
    "start_run",
]
