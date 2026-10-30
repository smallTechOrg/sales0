"""Discovery runs: eligibility, start (queue + submit to the runner), read models, recovery."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc, local_day_bounds_utc, local_now, utcnow
from sales0.config import get_settings
from sales0.db.engine import session_scope
from sales0.db.models import Keyword, LlmCall, ScrapeRun, ScrapeRunKeyword, StatusHistory, WatchRun
from sales0.errors import DomainError
from sales0.jobs.runner import get_runner, job_busy_error
from sales0.logging import get_logger
from sales0.services._common import not_found, now_or, record_history
from sales0.services import linkedin_accounts
from sales0.services.keywords import searchable_clause
from sales0.services.views import Eligibility, RunDetailView, RunKeywordView, RunView

if TYPE_CHECKING:
    from sales0.scraper.types import KeywordScrapeResult

log = get_logger("sales0.services.runs")

ACTIVE_STATUSES = ("queued", "running")
INTERRUPTED_MESSAGE = (
    "Interrupted by server restart — the next run continues with the keywords it had not reached"
)

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
        posts_relevant=run.posts_relevant,
        posts_skipped_ai=run.posts_skipped_ai,
        permalinks_found=run.permalinks_found,
        posts_repeat_skipped=run.posts_repeat_skipped or 0,
        relevance_pct=round(run.posts_relevant / run.posts_new * 100, 1) if run.posts_new else None,
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
        posts_relevant=row.posts_relevant,
        posts_skipped_ai=row.posts_skipped_ai,
        permalinks_found=row.permalinks_found,
        scrolls=row.scrolls,
        cost_usd_micros=row.cost_usd_micros,
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
def _account(db: Session, account_id: int | None) -> int:
    """The account a query is about: ``account_id`` or the default (first active) account."""
    return linkedin_accounts.default_account_id(db) if account_id is None else account_id


def _day_counts(
    db: Session, now: datetime, account_id: int | None = None, exclude_run_id: int | None = None
) -> tuple[int, int]:
    """(runs, posts seen) started today (local day) by ONE LinkedIn account. ``exclude_run_id``: a queued run being
    claimed must not count against its own daily allowance."""
    start, end = local_day_bounds_utc(get_settings().tz, now)
    in_day = (ScrapeRun.started_at >= start, ScrapeRun.started_at < end)
    mine = linkedin_accounts.account_runs_clause(db, _account(db, account_id))
    # Prospect-watch runs use the SAME daily post cap (their scanned posts count) but are not
    # discovery runs, so they never use up the daily run allowance.
    others = (ScrapeRun.id != exclude_run_id,) if exclude_run_id is not None else ()
    runs_today = db.scalar(
        select(func.count(ScrapeRun.id)).where(*in_day, mine, *others, ScrapeRun.id.not_in(select(WatchRun.run_id)))
    )
    posts_today = db.scalar(select(func.coalesce(func.sum(ScrapeRun.posts_seen), 0)).where(*in_day, mine))
    return int(runs_today or 0), int(posts_today or 0)


def posts_seen_today(db: Session, now: datetime | None = None, account_id: int | None = None) -> int:
    return _day_counts(db, now_or(now), account_id)[1]


def page_loads_today(db: Session, now: datetime | None = None, account_id: int | None = None) -> int:
    """LinkedIn page loads ONE account used today (local day): discovery AND watch runs (``scrape_runs.page_loads``)."""
    start, end = local_day_bounds_utc(get_settings().tz, now_or(now))
    mine = linkedin_accounts.account_runs_clause(db, _account(db, account_id))
    total = db.scalar(
        select(func.coalesce(func.sum(ScrapeRun.page_loads), 0)).where(
            ScrapeRun.started_at >= start, ScrapeRun.started_at < end, mine
        )
    )
    # q15: the laptop worker's one-page company-page visits (prospect research) count in the same daily total.
    from sales0.db.models import WorkerTask

    account = _account(db, account_id)
    first = linkedin_accounts.default_account_id(db) == account
    task_account = WorkerTask.linkedin_account_id == account
    tasks = db.scalar(
        select(func.coalesce(func.sum(WorkerTask.page_loads), 0)).where(
            WorkerTask.claimed_at >= start, WorkerTask.claimed_at < end,
            or_(task_account, WorkerTask.linkedin_account_id.is_(None)) if first else task_account,
        )
    )
    return int(total or 0) + int(tasks or 0)


def watch_day_counts(db: Session, now: datetime | None = None, account_id: int | None = None) -> tuple[int, int]:
    """(watch runs started, profile visits made) today by ONE account. A visit = a watch log row that was attempted."""
    start, end = local_day_bounds_utc(get_settings().tz, now_or(now))
    mine = linkedin_accounts.account_runs_clause(db, _account(db, account_id))
    in_day = (ScrapeRun.started_at >= start, ScrapeRun.started_at < end, mine)
    watch_runs = db.scalar(select(func.count(ScrapeRun.id)).where(*in_day, ScrapeRun.id.in_(select(WatchRun.run_id))))
    visits = db.scalar(
        select(func.count(ScrapeRunKeyword.id))
        .join(ScrapeRun, ScrapeRun.id == ScrapeRunKeyword.run_id)
        .where(
            *in_day,
            ScrapeRunKeyword.keyword_text.like("watch:%"),
            ScrapeRunKeyword.status.in_(("done", "warning", "error")),
        )
    )
    return int(watch_runs or 0), int(visits or 0)


def _active_keyword_count(db: Session) -> int:
    return int(db.scalar(select(func.count(Keyword.id)).where(searchable_clause())) or 0)


def safety_cooldown_until(
    db: Session, now: datetime | None = None, account_id: int | None = None
) -> datetime | None:
    """When Run now is allowed again after the latest LinkedIn safety stop of ONE account (UTC), or None.

    ``account_id`` None = the default account. Cool-downs are per account: one account being cooled down
    never blocks another. Reads the latest ``aborted`` run of that account (by ``finished_at``) whose ``abort_reason`` is a safety
    kind. rate_limited / captcha -> ``SALES0_SAFETY_COOLDOWN_MIN``; checkpoint / logout ->
    ``SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN``. None when that instant is not in the future
    or the applicable setting is 0. Derived from stored columns only (no migration): the latest aborted run
    AND the account's own ``last_stop_at`` (a stop found by a Re-check), whichever ends later.
    """
    now = ensure_utc(now_or(now))
    row = db.execute(
        select(ScrapeRun.abort_reason, ScrapeRun.finished_at)
        .where(
            ScrapeRun.status == "aborted",
            ScrapeRun.abort_reason.in_(LONG_COOLDOWN_KINDS + SHORT_COOLDOWN_KINDS),
            ScrapeRun.finished_at.is_not(None),
            linkedin_accounts.account_runs_clause(db, _account(db, account_id)),
        )
        .order_by(ScrapeRun.finished_at.desc(), ScrapeRun.id.desc())
        .limit(1)
    ).first()
    candidates: list[datetime] = []
    if row is not None:
        kind, finished_at = row
        candidates.append(ensure_utc(finished_at) + timedelta(minutes=_cooldown_minutes(kind)))
    # Fixscrape D #4: a stop detected by a Re-check (no aborted run behind it) also starts the cool-down:
    # the account's own ``last_stop_at`` / ``safety_state``. A stop "in the future" of ``now`` is ignored.
    account = linkedin_accounts.get_account(db, _account(db, account_id))
    if account is not None and account.last_stop_at is not None and account.safety_state in (
        LONG_COOLDOWN_KINDS + SHORT_COOLDOWN_KINDS
    ):
        stopped = ensure_utc(account.last_stop_at)
        if stopped <= now:
            candidates.append(stopped + timedelta(minutes=_cooldown_minutes(account.safety_state)))
    until = max(candidates, default=None)
    return until if until is not None and until > now else None


def _cooldown_minutes(kind: str) -> int:
    settings = get_settings()
    return settings.safety_cooldown_min if kind in LONG_COOLDOWN_KINDS else settings.safety_cooldown_checkpoint_min


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


def _check(
    db: Session, now: datetime, account_id: int | None = None, *, watch: bool = False, ignore_run_id: int | None = None
) -> tuple[DomainError | None, int, int]:
    """The first reason a run cannot start (api.md order), plus today's counts (all for ONE account).

    ``watch=True`` (prospect-watch run): same overlap / session / cool-down / post-cap rules, but it
    needs no keywords and does not consume the daily run allowance.

    Hosted mode: there is no local browser, so the LinkedIn connection state and the job runner are not
    consulted (the laptop worker owns the browser and reports what it finds); the caps and the cool-down still
    apply, both when a run is queued and again when a worker claims it (``ignore_run_id`` = the queued run itself)."""
    settings = get_settings()
    hosted = settings.is_hosted
    account_id = _account(db, account_id)
    runs_today, posts_today = _day_counts(db, now, account_id, ignore_run_id)
    error: DomainError | None = None
    session = linkedin_accounts.get_account(db, account_id)
    busy = None if hosted else get_runner().busy()
    active_q = select(ScrapeRun.id).where(ScrapeRun.status.in_(ACTIVE_STATUSES))
    if ignore_run_id is not None:
        active_q = active_q.where(ScrapeRun.id != ignore_run_id)
    has_active = db.scalar(active_q.limit(1)) is not None
    if has_active or busy == "run":
        error = DomainError("run_in_progress", "A run is already in progress.", 409)
    elif session.disabled:
        error = DomainError("account_disabled", f"LinkedIn account '{session.label}' is disabled - enable it in Setup.", 409)
    elif not hosted and session.status == "needs_attention":
        detail = (session.last_error or "resolve the LinkedIn warning").rstrip(". ")
        error = DomainError(
            "needs_attention",
            f"LinkedIn needs attention: {detail}. Press Connect LinkedIn in Setup — the sales0 Chrome window "
            "opens on LinkedIn so you can resolve it.",
            409,
        )
    elif (cooldown := safety_cooldown_until(db, now, account_id)) is not None:
        error = DomainError("safety_cooldown", cooldown_reason(cooldown, now), 429)
    elif not hosted and session.status != "connected":
        error = DomainError("not_connected", "Connect LinkedIn first (Setup → Connect LinkedIn).", 409)
    elif busy is not None:
        error = job_busy_error(busy)
    elif not watch and _active_keyword_count(db) == 0:
        error = DomainError("no_keywords", "Add at least one active keyword in Setup.", 409)
    elif not watch and runs_today >= settings.scrape_max_runs_per_day:
        error = DomainError(
            "daily_run_cap",
            f"Daily run limit reached ({runs_today}/{settings.scrape_max_runs_per_day}). "
            f"Runs reset at midnight ({settings.timezone}).",
            429,
        )
    elif watch and watch_day_counts(db, now, account_id)[0] >= settings.watch_max_runs_per_day:
        error = DomainError(
            "daily_watch_cap",
            f"Daily prospect-watch run limit reached ({watch_day_counts(db, now, account_id)[0]}/"
            f"{settings.watch_max_runs_per_day}). Runs reset at midnight ({settings.timezone}).",
            429,
        )
    elif posts_today >= settings.scrape_max_posts_per_day:
        error = DomainError(
            "daily_post_cap",
            f"Daily post limit reached ({posts_today}/{settings.scrape_max_posts_per_day}) "
            "— protects your LinkedIn account.",
            429,
        )
    elif (loads := page_loads_today(db, now, account_id)) >= settings.scrape_max_pageloads_per_day:
        error = DomainError(
            "daily_pageload_cap",
            f"Daily LinkedIn page-load limit reached ({loads}/{settings.scrape_max_pageloads_per_day}) "
            "— protects your LinkedIn account. Resets at midnight "
            f"({settings.timezone}).",
            429,
        )
    elif watch and (visits := watch_day_counts(db, now, account_id)[1]) >= settings.watch_max_visits_per_day:
        error = DomainError(
            "daily_visit_cap",
            f"Daily profile-visit limit reached ({visits}/{settings.watch_max_visits_per_day}). "
            f"Resets at midnight ({settings.timezone}).",
            429,
        )
    return error, runs_today, posts_today


def run_eligibility(db: Session, now: datetime | None = None, account_id: int | None = None) -> Eligibility:
    settings = get_settings()
    error, runs_today, posts_today = _check(db, now_or(now), account_id)
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
def start_run(db: Session, actor_user_id: int | None = None, linkedin_account_id: int | None = None) -> RunView:
    """Queue a run for one LinkedIn account (default: the first active) and hand it to the job runner.
    Commits so the job thread can see the row."""
    from sales0.jobs import discovery_job

    now = utcnow()
    account_id = _account(db, linkedin_account_id)
    error, _, _ = _check(db, now, account_id)
    if error is not None:
        raise error
    settings = get_settings()
    hosted = settings.is_hosted
    run = ScrapeRun(
        trigger="manual",
        mode=settings.linkedin_mode,
        status="queued",
        started_at=now,
        current_step=(
            "Queued — waiting for the laptop worker (`sales0 worker`)" if hosted else "Queued — starting the browser"
        ),
        actor_user_id=actor_user_id,
        linkedin_account_id=account_id,
    )
    db.add(run)
    db.flush()
    record_history(db, "scrape_run", run.id, "status", None, "queued", actor_user_id=actor_user_id, now=now)
    db.commit()
    run_id = run.id
    if hosted:
        return run_view(run)  # a laptop worker claims it (services.worker_service.claim); nothing runs in this process
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


def sync_account_state(db: Session, account_id: int | None = None, now: datetime | None = None) -> None:
    """Snapshot one account's derived daily counters and cool-down onto its ``linkedin_accounts`` row.

    The runs stay authoritative (eligibility always recomputes from them); the row mirrors them so the
    Setup list and exports can show per-account state without a query per account.
    """
    now = now_or(now)
    account_id = _account(db, account_id)
    runs_today, posts_today = _day_counts(db, now, account_id)
    linkedin_accounts.store_counters(
        db,
        account_id,
        day=local_now(get_settings().tz, now).date().isoformat(),
        runs_today=runs_today,
        posts_today=posts_today,
        cooldown_until=safety_cooldown_until(db, now, account_id),
    )


def plan_keywords(db: Session, limit: int) -> list[tuple[int, str]]:
    """Active keywords for the next run: first those the latest finished run had not reached
    (keyword row pending/running/skipped and ``last_run_at`` before that run started), then never-run,
    then least-recently-run, then by id. Queued/running runs (the run being planned) are ignored."""
    limit = max(int(limit), 0)
    if limit == 0:
        return []
    chosen: list[tuple[int, str]] = []
    seen: set[int] = set()
    previous = db.scalar(
        select(ScrapeRun)
        .where(ScrapeRun.status.not_in(ACTIVE_STATUSES), ScrapeRun.id.not_in(select(WatchRun.run_id)))
        .order_by(ScrapeRun.id.desc())
        .limit(1)
    )
    if previous is not None:
        rows = db.execute(
            select(Keyword.id, Keyword.text, Keyword.last_run_at)
            .join(ScrapeRunKeyword, ScrapeRunKeyword.keyword_id == Keyword.id)
            .where(
                ScrapeRunKeyword.run_id == previous.id,
                ScrapeRunKeyword.status.in_(("pending", "running", "skipped")),
                searchable_clause(),
            )
            .order_by(ScrapeRunKeyword.id)
        ).all()
        for kid, text, last_run_at in rows:
            if previous.started_at is not None and last_run_at is not None:
                if ensure_utc(last_run_at) >= ensure_utc(previous.started_at):
                    continue
            if kid not in seen:
                seen.add(kid)
                chosen.append((int(kid), text))
    if len(chosen) < limit:
        rest = db.execute(
            select(Keyword.id, Keyword.text)
            .where(searchable_clause())
            .order_by(case((Keyword.last_run_at.is_(None), 0), else_=1), Keyword.last_run_at, Keyword.id)
        ).all()
        for kid, text in rest:
            if kid not in seen:
                seen.add(kid)
                chosen.append((int(kid), text))
    return chosen[:limit]


def recover_interrupted_runs(db: Session) -> int:
    """Mark runs left queued/running by a previous process as ``aborted`` (no abort reason, so no
    cool-down) and their unfinished keyword rows ``skipped``. Returns the count."""
    now = utcnow()
    runs = db.scalars(select(ScrapeRun).where(ScrapeRun.status.in_(ACTIVE_STATUSES))).all()
    for run in runs:
        record_history(db, "scrape_run", run.id, "status", run.status, "aborted", note=INTERRUPTED_MESSAGE, now=now)
        run.status = "aborted"
        run.abort_reason = None
        run.error_message = INTERRUPTED_MESSAGE
        run.finished_at = now
        run.current_step = INTERRUPTED_MESSAGE[:200]
    if runs:
        ids = [r.id for r in runs]
        db.execute(
            update(ScrapeRunKeyword)
            .where(ScrapeRunKeyword.run_id.in_(ids), ScrapeRunKeyword.status.in_(("pending", "running")))
            .values(status="skipped", error_message=INTERRUPTED_MESSAGE[:300])
        )
    db.flush()
    return len(runs)


def recover_interrupted_runs_on_startup() -> int:
    """Startup hook (web/app.py lifespan): own session, committed, logged.

    Hosted: a restart (scale to zero, a new revision) interrupts nothing, because no run lives in this process; a
    queued run waits for the worker and a claimed one is judged by its heartbeat (``worker_service.reap_stale``)."""
    if get_settings().is_hosted:
        return 0
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
    "plan_keywords",
    "posts_seen_today",
    "recover_interrupted_runs",
    "recover_interrupted_runs_on_startup",
    "run_eligibility",
    "run_view",
    "safety_cooldown_until",
    "start_run",
    "sync_account_state",
]
