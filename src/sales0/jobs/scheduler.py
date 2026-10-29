"""In-process scheduler (Phase 4): named jobs, persisted state, fake-clock friendly.

Design
------
* One daemon thread calls :meth:`Scheduler.tick` every ``SALES0_SCHEDULER_POLL_S``. ``tick(now)``
  is the only scheduling primitive and takes an injectable clock/RNG, so tests drive it
  deterministically. The thread is started/stopped in the FastAPI lifespan.
* State lives in the ``scheduled_jobs`` table (enable flag, next run, last result, job-private
  ``state_json``), so enable/disable choices, the planned nightly slots and "already built
  today's digest" survive restarts.
* Jobs: ``discovery_run`` (nightly window), ``daily_digest``, ``sheet_sync`` (delegates to the
  existing ``SheetSyncRunner``), ``purge`` (retention hooks).

Missed-run policy (no catch-up storms)
--------------------------------------
* discovery_run: a slot whose time passed while the app was down still runs ONCE if the
  nightly window is still open; once the window has closed it is recorded ``missed`` and
  forgotten - the next opportunity is tomorrow's window. Never more than the configured
  runs/day, never two starts for one slot.
* daily_digest / purge: if today's run has not happened and its time has passed, they run once
  on the next tick (idempotent); earlier days are never back-filled.
* sheet_sync: the existing runner decides (last finish + interval, first run shortly after boot).

Overlap: scheduled discovery goes through ``start_scheduled_discovery()`` (jobs/scheduled_discovery.py)
which uses the same run-start service as the Run now button, so it can never overlap a manual
run and always respects the daily run/post caps, the safety cool-down, the LinkedIn session
state and the monthly AI spend cap. Every refusal is recorded as a ``skipped`` result with the
reason.
"""

from __future__ import annotations

import json
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc, utcnow
from sales0.config import Settings, get_settings
from sales0.db.models import Digest, ScheduledJob, Suggestion
from sales0.logging import get_logger

log = get_logger("sales0.scheduler")

RETRY_MINUTES = 10
MAX_ATTEMPTS = 6
WINDOW_TAIL_MARGIN = timedelta(minutes=30)  # nothing starts in the last 30 min of the window
MIN_SLOT_SPAN = timedelta(minutes=5)

Clock = Callable[[], datetime]
SessionFactory = Callable[[], Session]


# ---------------------------------------------------------------------- results / views
@dataclass(frozen=True)
class JobResult:
    status: str  # ok | skipped | failed
    detail: str = ""
    run_id: int | None = None


@dataclass
class JobView:
    name: str
    label: str
    description: str
    schedule_text: str
    enabled: bool
    next_run_at: datetime | None
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_status: str | None
    last_detail: str | None
    last_run_id: int | None
    runs_total: int
    note: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------- time helpers
def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def local_at(settings: Settings, day: date, at: time) -> datetime:
    """The UTC instant of local ``day`` at ``at``."""
    return datetime.combine(day, at, tzinfo=settings.tz).astimezone(UTC)


def local_day(settings: Settings, now: datetime) -> date:
    return ensure_utc(now).astimezone(settings.tz).date()


# ---------------------------------------------------------------------- job definitions
@dataclass
class Ctx:
    settings: Settings
    sessions: SessionFactory
    rng: random.Random


class JobDef:
    name = ""
    label = ""
    description = ""
    default_enabled = False
    delegated = False  # True: the job runs itself elsewhere (sheet_sync); tick just pokes it

    def schedule_text(self, ctx: Ctx) -> str:
        return ""

    def plan(self, ctx: Ctx, state: dict[str, Any], now: datetime) -> datetime | None:
        """Update ``state`` in place if needed and return the next run time (None = nothing planned)."""
        raise NotImplementedError

    def execute(self, ctx: Ctx, state: dict[str, Any], now: datetime, *, manual: bool) -> JobResult:
        raise NotImplementedError

    def note(self, ctx: Ctx) -> str | None:
        return None


class DiscoveryJob(JobDef):
    name = "discovery_run"
    label = "Nightly discovery run"
    description = (
        "Starts one LinkedIn discovery run at a random time inside the nightly window, within the daily "
        "run/post caps and cool-downs. Never overlaps a manual run; skips (and says why) when LinkedIn needs attention."
    )
    default_enabled = False  # opt-in: it drives your logged-in LinkedIn account

    def schedule_text(self, ctx: Ctx) -> str:
        s = ctx.settings
        n = min(s.scheduler_discovery_max_runs_per_day, s.scrape_max_runs_per_day)
        return f"{s.scheduler_window_start}-{s.scheduler_window_end} {s.timezone}, up to {n} run{'s' if n != 1 else ''}/day, jittered"

    # -- planning
    @staticmethod
    def _window(ctx: Ctx, day: date) -> tuple[datetime, datetime]:
        s = ctx.settings
        return local_at(s, day, parse_hhmm(s.scheduler_window_start)), local_at(s, day, parse_hhmm(s.scheduler_window_end))

    def _make_slots(self, ctx: Ctx, day: date, now: datetime) -> list[dict[str, Any]]:
        start, end = self._window(ctx, day)
        end = end - WINDOW_TAIL_MARGIN if end - start > 2 * WINDOW_TAIL_MARGIN else end
        start = max(start, now)
        if end - start < MIN_SLOT_SPAN:
            return []
        s = ctx.settings
        n = max(1, min(s.scheduler_discovery_max_runs_per_day, s.scrape_max_runs_per_day))
        seg = (end - start) / n
        slots = []
        for i in range(n):
            at = start + seg * i + timedelta(seconds=ctx.rng.uniform(0, seg.total_seconds()))
            slots.append({"at": at.isoformat(), "status": "pending", "attempts": 0})
        return slots

    def plan(self, ctx: Ctx, state: dict[str, Any], now: datetime) -> datetime | None:
        today = local_day(ctx.settings, now)
        day = date.fromisoformat(state["day"]) if state.get("day") else None
        # A closed window forgets its unfinished slots: recorded as missed, never caught up.
        if day is not None and now > self._window(ctx, day)[1]:
            for slot in state.get("slots", []):
                if slot["status"] == "pending":
                    slot["status"] = "missed"
                    state["missed"] = state.get("missed", 0) + 1
        pending = [s for s in state.get("slots", []) if s["status"] == "pending"]
        if pending:
            return min(datetime.fromisoformat(s["at"]) for s in pending)
        if day != today and now <= self._window(ctx, today)[1]:
            slots = self._make_slots(ctx, today, now)
            if slots:
                state.update(day=today.isoformat(), slots=slots)
                return min(datetime.fromisoformat(s["at"]) for s in slots)
        tomorrow = today + timedelta(days=1)
        slots = self._make_slots(ctx, tomorrow, now)
        state.update(day=tomorrow.isoformat(), slots=slots)
        return min((datetime.fromisoformat(s["at"]) for s in slots), default=None)

    def execute(self, ctx: Ctx, state: dict[str, Any], now: datetime, *, manual: bool) -> JobResult:
        from sales0.jobs.scheduled_discovery import start_scheduled_discovery

        outcome = start_scheduled_discovery()
        due = None
        if not manual:
            due = min(
                (s for s in state.get("slots", []) if s["status"] == "pending" and datetime.fromisoformat(s["at"]) <= now),
                key=lambda s: s["at"],
                default=None,
            )
        if outcome.started:
            if due is not None:
                due["status"] = "started"
            return JobResult("ok", f"Started run #{outcome.run_id}" + (" (run now)" if manual else ""), outcome.run_id)
        reason = outcome.reason or outcome.code or "could not start"
        if due is not None:
            due["attempts"] = due.get("attempts", 0) + 1
            _, win_end = self._window(ctx, date.fromisoformat(state["day"]))
            retry_at = outcome.retry_at or (now + timedelta(minutes=RETRY_MINUTES))
            if outcome.retryable and due["attempts"] < MAX_ATTEMPTS and retry_at < win_end:
                due["at"] = max(retry_at, now + timedelta(minutes=1)).isoformat()
                return JobResult("skipped", f"{reason} - will try again at {due['at'][11:16]} UTC if the window is still open")
            due["status"] = "skipped"
            due["detail"] = reason[:200]
        return JobResult("skipped", reason)


class DailyJob(JobDef):
    """Runs once per local day at a fixed time. Subclasses implement ``_run(ctx, now)``."""

    time_setting = ""

    def _at(self, ctx: Ctx) -> time:
        return parse_hhmm(getattr(ctx.settings, self.time_setting))

    def schedule_text(self, ctx: Ctx) -> str:
        return f"daily at {getattr(ctx.settings, self.time_setting)} {ctx.settings.timezone}"

    def plan(self, ctx: Ctx, state: dict[str, Any], now: datetime) -> datetime | None:
        today = local_day(ctx.settings, now)
        if state.get("last_date") == today.isoformat():
            today += timedelta(days=1)
        nxt = local_at(ctx.settings, today, self._at(ctx))
        retry = state.get("retry_at")
        return max(nxt, datetime.fromisoformat(retry)) if retry else nxt

    def execute(self, ctx: Ctx, state: dict[str, Any], now: datetime, *, manual: bool) -> JobResult:
        try:
            result = self._run(ctx, now)
        except Exception as exc:  # noqa: BLE001
            log.exception("scheduled_job_crashed", job=self.name)
            result = JobResult("failed", f"{type(exc).__name__}: {str(exc)[:200]}")
        if manual:
            return result
        today = local_day(ctx.settings, now).isoformat()
        if result.status == "ok":
            state.update(last_date=today, fails=0)
            state.pop("retry_at", None)
        else:  # back off 15 min; give up for the day after 4 failures (no failure storms)
            fails = (state.get("fails", 0) if state.get("fails_day") == today else 0) + 1
            state.update(fails=fails, fails_day=today)
            if fails >= 4:
                state.update(last_date=today)
                state.pop("retry_at", None)
            else:
                state["retry_at"] = (now + timedelta(minutes=15)).isoformat()
        return result

    def _run(self, ctx: Ctx, now: datetime) -> JobResult:
        raise NotImplementedError


class DigestJob(DailyJob):
    name = "daily_digest"
    label = "Daily digest"
    description = "Builds and stores the daily digest (never emails anyone; Gmail draft-to-self is a button on the Digest page)."
    default_enabled = True
    time_setting = "scheduler_digest_time"

    def _run(self, ctx: Ctx, now: datetime) -> JobResult:
        from sales0.services import digest

        with ctx.sessions() as db:
            view = digest.generate_digest(db, trigger="scheduled", now=now)
            db.commit()
            return JobResult("ok", f"Built the digest for {view.digest_date}")


class WatchJob(DailyJob):
    name = "profile_watch"
    label = "Prospect watch"
    description = (
        "Visits up to N tracked LinkedIn profiles (each at most once per few days, least recently checked first) "
        "for new posts, and triages them like discovery. Never overlaps a discovery run; shares the safety "
        "cool-down and the daily post cap."
    )
    default_enabled = False  # opt-in: it drives your logged-in LinkedIn account
    time_setting = "scheduler_watch_time"

    def schedule_text(self, ctx: Ctx) -> str:
        s = ctx.settings
        jitter = f" (+0-{s.watch_start_jitter_min} min)" if s.watch_start_jitter_min else ""
        return (
            f"daily at {s.scheduler_watch_time}{jitter} {s.timezone}, up to {s.watch_max_profiles_per_run} profiles, "
            f"each at most every {s.watch_min_days_between_checks:g} days"
        )

    def plan(self, ctx: Ctx, state: dict[str, Any], now: datetime) -> datetime | None:
        """Today's slot if still ahead, else tomorrow's: unlike the digest this drives the LinkedIn
        account, so enabling it after the time (or a finished day) never starts an immediate catch-up."""
        retry = state.get("retry_at")
        if retry:
            return datetime.fromisoformat(retry)
        today = local_day(ctx.settings, now)
        slot = self._slot(ctx, today)
        if state.get("last_date") == today.isoformat() or slot <= now:
            slot = self._slot(ctx, today + timedelta(days=1))
        return slot

    def _slot(self, ctx: Ctx, day: date) -> datetime:
        """The configured time plus a jitter (0..watch_start_jitter_min minutes), derived from the date only:
        stable across restarts and reproducible in tests, but different every day (fixscrape D #14)."""
        slot = local_at(ctx.settings, day, self._at(ctx))
        window = ctx.settings.watch_start_jitter_min * 60
        if window <= 0:
            return slot
        return slot + timedelta(seconds=random.Random(f"{self.name}:{day.isoformat()}").randint(0, window))

    def _run(self, ctx: Ctx, now: datetime) -> JobResult:
        from sales0.jobs.scheduled_watch import start_scheduled_watch

        outcome = start_scheduled_watch()
        if outcome.started:
            return JobResult("ok", f"Started watch run #{outcome.run_id}", outcome.run_id)
        if outcome.code == "nothing_due":
            return JobResult("ok", "No tracked profile is due for a check")
        return JobResult("skipped", outcome.reason or outcome.code or "could not start")


PurgeHook = Callable[[Session, datetime], int]
_purge_hooks: dict[str, PurgeHook] = {}


def register_purge_hook(name: str, hook: PurgeHook, *, replace: bool = True) -> None:
    """Register ``hook(db, cutoff) -> rows_removed``; the purge job calls every hook (Phase 2 adds its own)."""
    if name in _purge_hooks and not replace:
        raise ValueError(f"purge hook {name!r} already registered")
    _purge_hooks[name] = hook


def unregister_purge_hook(name: str) -> None:
    _purge_hooks.pop(name, None)


def purge_hooks() -> dict[str, PurgeHook]:
    return dict(_purge_hooks)


def _purge_digests(db: Session, cutoff: datetime) -> int:
    return db.execute(delete(Digest).where(Digest.created_at < cutoff)).rowcount or 0


def _purge_decided_suggestions(db: Session, cutoff: datetime) -> int:
    # Accepted/dismissed suggestions are kept long enough to stay idempotent (never re-suggested)
    # for the retention period, then dropped.
    return db.execute(delete(Suggestion).where(Suggestion.status != "pending", Suggestion.decided_at < cutoff)).rowcount or 0


register_purge_hook("digests", _purge_digests)
register_purge_hook("decided_suggestions", _purge_decided_suggestions)
# Fixscrape D (#6 wiring): seen_posts raw text retention (backfills NULL purge dates; keeps pending/failed text).
from sales0.jobs import purge_job  # noqa: E402  (purge_job does not import the scheduler)

register_purge_hook(purge_job.HOOK_NAME, purge_job.purge_hook)


class PurgeJob(DailyJob):
    name = "purge"
    label = "Retention purge"
    description = "Runs the registered purge hooks (old digests, decided suggestions, seen-post raw text past its retention; more can be registered) beyond the retention period."
    default_enabled = True
    time_setting = "scheduler_purge_time"

    def _run(self, ctx: Ctx, now: datetime) -> JobResult:
        cutoff = now - timedelta(days=ctx.settings.purge_retention_days)
        parts: list[str] = []
        failed = 0
        for name, hook in purge_hooks().items():
            try:
                with ctx.sessions() as db:
                    removed = hook(db, cutoff)
                    db.commit()
                parts.append(f"{name}={removed}")
            except Exception as exc:  # noqa: BLE001 - one bad hook must not stop the others
                failed += 1
                parts.append(f"{name}=ERROR({type(exc).__name__})")
        return JobResult("failed" if failed else "ok", "removed " + ", ".join(parts))


class FollowUpDraftsJob(DailyJob):
    name = "followup_drafts"
    label = "Draft due follow-up emails"
    description = (
        "Writes AI draft emails for due Email follow-ups into the outbox (local mode, opt-in, default OFF). It never "
        "approves or sends anything: every draft waits for a person to review, approve and send it."
    )
    default_enabled = False
    time_setting = "scheduler_followup_drafts_time"

    def _run(self, ctx: Ctx, now: datetime) -> JobResult:
        from sales0.services import followup_drafts

        with ctx.sessions() as db:
            result = followup_drafts.draft_due(db, None, None, now=now)
            db.commit()
            return JobResult("ok", result.summary())


class SheetSyncJob(JobDef):
    name = "sheet_sync"
    label = "Google Sheet sync"
    description = "Runs the existing read-only Google Sheet sync on its interval (SALES0_SHEET_SYNC_MINUTES; 0 = never)."
    default_enabled = False
    delegated = True

    def __init__(self, runner_factory: Callable[[Settings], Any] | None = None) -> None:
        self._runner_factory = runner_factory
        self._runner: Any = None

    def runner(self, ctx: Ctx) -> Any:
        if self._runner is None:
            if self._runner_factory is not None:
                self._runner = self._runner_factory(ctx.settings)
            else:
                from sales0.jobs.sheet_sync_job import build_runner

                self._runner = build_runner(ctx.settings)
        return self._runner

    def available(self) -> bool:
        if self._runner_factory is not None:
            return True
        from sales0.jobs.sheet_sync_job import repository_available

        return repository_available()

    def schedule_text(self, ctx: Ctx) -> str:
        m = ctx.settings.sheet_sync_minutes
        return f"every {m} min (about)" if m > 0 else "interval is 0 - never"

    def note(self, ctx: Ctx) -> str | None:
        if ctx.settings.sheet_sync_minutes <= 0:
            return "SALES0_SHEET_SYNC_MINUTES is 0, so the interval is off; Run now still works."
        if not self.available():
            from sales0.jobs.sheet_sync_job import NO_REPOSITORY

            return NO_REPOSITORY
        return None

    def plan(self, ctx: Ctx, state: dict[str, Any], now: datetime) -> datetime | None:
        if ctx.settings.sheet_sync_minutes <= 0 or not self.available():
            return None
        return self.runner(ctx).status().next_run_at

    def execute(self, ctx: Ctx, state: dict[str, Any], now: datetime, *, manual: bool) -> JobResult:
        if not self.available():
            return JobResult("skipped", self.note(ctx) or "not available")
        started = self.runner(ctx).trigger_now()
        return JobResult("ok" if started else "skipped", "Sync started" if started else "A sync is already running")

    def poke(self, ctx: Ctx) -> None:
        """Called every tick while the job is enabled: let the runner start a due sync."""
        if ctx.settings.sheet_sync_minutes > 0 and self.available():
            self.runner(ctx).tick()

    def mirror(self, ctx: Ctx, row: ScheduledJob) -> None:
        """Copy the runner's last-run status onto the row (the runner persists its own state too)."""
        if self._runner is None:
            return
        st = self._runner.status()
        row.next_run_at = st.next_run_at if row.enabled and ctx.settings.sheet_sync_minutes > 0 else None
        if st.last_started_at:
            row.last_started_at, row.last_finished_at = st.last_started_at, st.last_finished_at
            row.last_status = "running" if st.running else ("ok" if st.last_ok else "failed")
            row.last_detail = (st.last_error or ("synced" if st.last_ok else ""))[:500] or None
            row.runs_total = max(row.runs_total, st.runs)

    def stop(self) -> None:
        if self._runner is not None:
            self._runner.stop(1.0)


# ---------------------------------------------------------------------- scheduler
class Scheduler:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        sessions: SessionFactory | None = None,
        now: Clock = utcnow,
        rng: random.Random | None = None,
        jobs: list[JobDef] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if sessions is None:
            from sales0.db.engine import get_sessionmaker

            sessions = get_sessionmaker()
        self.ctx = Ctx(self.settings, sessions, rng or random.Random())
        self.now = now
        self.defs: dict[str, JobDef] = {j.name: j for j in (jobs or [DiscoveryJob(), WatchJob(), DigestJob(), SheetSyncJob(), PurgeJob(), FollowUpDraftsJob()])}
        self._lock = threading.RLock()  # one job body at a time (tick / run_now)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ persistence
    def _row(self, db: Session, name: str) -> ScheduledJob:
        row = db.scalar(select(ScheduledJob).where(ScheduledJob.name == name))
        if row is None:
            row = ScheduledJob(name=name, enabled=self.defs[name].default_enabled, state_json="{}")
            db.add(row)
            db.flush()
        return row

    @staticmethod
    def _state(row: ScheduledJob) -> dict[str, Any]:
        try:
            data = json.loads(row.state_json or "{}")
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def _replan(self, row: ScheduledJob, state: dict[str, Any], now: datetime) -> None:
        job = self.defs[row.name]
        row.next_run_at = job.plan(self.ctx, state, now) if row.enabled else None
        row.state_json = json.dumps(state)

    def ensure_rows(self) -> None:
        """Create missing rows and plan enabled jobs that have no next run (boot / first page view)."""
        now = self.now()
        with self._lock, self.ctx.sessions() as db:
            for name in self.defs:
                row = self._row(db, name)
                if row.enabled and row.next_run_at is None:
                    self._replan(row, self._state(row), now)
            db.commit()

    # ------------------------------------------------------------------ ticking
    def tick(self) -> list[str]:
        """Run every job that is due. Returns the names that fired. Never raises."""
        fired: list[str] = []
        try:
            now = self.now()
            with self._lock:
                for name in list(self.defs):
                    if self._tick_one(name, now):
                        fired.append(name)
        except Exception:  # noqa: BLE001 - the loop must survive anything
            log.exception("scheduler_tick_failed")
        return fired

    def _tick_one(self, name: str, now: datetime) -> bool:
        job = self.defs[name]
        with self.ctx.sessions() as db:
            row = self._row(db, name)
            state = self._state(row)
            if job.delegated:
                if row.enabled and isinstance(job, SheetSyncJob):
                    job.poke(self.ctx)
                if isinstance(job, SheetSyncJob):
                    job.mirror(self.ctx, row)
                db.commit()
                return False
            if not row.enabled:
                if row.next_run_at is not None:
                    row.next_run_at = None
                    db.commit()
                return False
            if row.next_run_at is None:
                self._replan(row, state, now)
                db.commit()
            nxt = row.next_run_at
            if nxt is None or ensure_utc(nxt) > now:
                return False
            return self._fire(db, row, state, now, manual=False)

    def _fire(self, db: Session, row: ScheduledJob, state: dict[str, Any], now: datetime, *, manual: bool) -> bool:
        job = self.defs[row.name]
        # Window closed while we were away: record "missed" instead of starting late (no catch-up).
        if isinstance(job, DiscoveryJob) and not manual:
            day = state.get("day")
            if day and now > job._window(self.ctx, date.fromisoformat(day))[1]:
                before = state.get("missed", 0)
                self._replan(row, state, now)
                if state.get("missed", 0) > before:
                    row.last_status, row.last_finished_at = "missed", now
                    row.last_detail = "Window closed before the planned time (app was down or busy); not caught up."
                db.commit()
                return False
        row.last_started_at, row.last_status, row.last_detail = now, "running", None
        db.commit()
        try:
            result = job.execute(self.ctx, state, now, manual=manual)
        except Exception as exc:  # noqa: BLE001
            log.exception("scheduled_job_crashed", job=row.name)
            result = JobResult("failed", f"{type(exc).__name__}: {str(exc)[:200]}")
        row.last_finished_at = self.now()
        row.last_status, row.last_detail, row.last_run_id = result.status, result.detail[:500], result.run_id
        if not job.delegated:
            row.runs_total = (row.runs_total or 0) + 1
        if manual:
            row.state_json = json.dumps(state)  # keep the existing plan; only slot bookkeeping changes
        else:
            self._replan(row, state, now)
        db.commit()
        log.info("scheduled_job_done", job=row.name, status=result.status, manual=manual)
        return True

    # ------------------------------------------------------------------ controls (pages)
    def set_enabled(self, name: str, enabled: bool) -> None:
        if name not in self.defs:
            raise KeyError(name)
        now = self.now()
        with self._lock, self.ctx.sessions() as db:
            row = self._row(db, name)
            row.enabled = enabled
            if enabled:
                self._replan(row, self._state(row), now)
            else:
                row.next_run_at = None
            db.commit()

    def run_now(self, name: str) -> JobResult:
        """Run a job immediately (ignores its schedule/window, not its safety checks)."""
        if name not in self.defs:
            raise KeyError(name)
        now = self.now()
        with self._lock, self.ctx.sessions() as db:
            row = self._row(db, name)
            state = self._state(row)
            self._fire(db, row, state, now, manual=True)
            db.refresh(row)
            return JobResult(row.last_status or "ok", row.last_detail or "", row.last_run_id)

    def snapshot(self) -> list[JobView]:
        self.ensure_rows()
        views: list[JobView] = []
        with self.ctx.sessions() as db:
            for name, job in self.defs.items():
                row = self._row(db, name)
                state = self._state(row)
                if isinstance(job, SheetSyncJob):
                    job.mirror(self.ctx, row)
                extra: dict[str, Any] = {}
                if isinstance(job, DiscoveryJob):
                    extra["slots"] = state.get("slots", [])
                    extra["slots_day"] = state.get("day")
                    extra["missed"] = state.get("missed", 0)
                views.append(
                    JobView(
                        name=name, label=job.label, description=job.description, schedule_text=job.schedule_text(self.ctx),
                        enabled=row.enabled, next_run_at=row.next_run_at if row.enabled else None,
                        last_started_at=row.last_started_at, last_finished_at=row.last_finished_at,
                        last_status=row.last_status, last_detail=row.last_detail, last_run_id=row.last_run_id,
                        runs_total=row.runs_total or 0, note=job.note(self.ctx), extra=extra,
                    )
                )
            db.commit()
        return views

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self.ensure_rows()
        self._stop.clear()
        poll = self.settings.scheduler_poll_s

        def loop() -> None:
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(poll)

        self._thread = threading.Thread(target=loop, name="sales0-scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler_started", jobs=list(self.defs))

    def stop(self, wait_s: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(wait_s)
            self._thread = None
        for job in self.defs.values():
            if isinstance(job, SheetSyncJob):
                job.stop()

    @property
    def running(self) -> bool:
        return self._thread is not None


_lock = threading.Lock()
_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _scheduler
    with _lock:
        if _scheduler is None:
            _scheduler = Scheduler()
        return _scheduler


def reset_scheduler() -> None:
    """Test seam: stop and drop the singleton."""
    global _scheduler
    with _lock:
        if _scheduler is not None:
            _scheduler.stop(1.0)
        _scheduler = None


__all__ = [
    "DiscoveryJob", "DigestJob", "FollowUpDraftsJob", "JobResult", "JobView", "PurgeJob", "Scheduler", "SheetSyncJob", "WatchJob",
    "get_scheduler", "register_purge_hook", "reset_scheduler", "unregister_purge_hook",
]
