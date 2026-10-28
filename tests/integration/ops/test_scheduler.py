"""Scheduler: window math, jitter bounds, no-overlap, restart persistence, cap respect, missed-run
policy, digest/purge/sheet_sync wiring. A fake clock + seeded RNG drive ``tick()``; the only fake
is the discovery START (``start_scheduled_discovery``) in the pure-policy tests; the adapter
itself is exercised against the REAL run-start service with real DB state."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from sales0.config import reset_settings
from sales0.db.engine import get_sessionmaker, session_scope
from sales0.db.models import Digest, LinkedInSession, LlmCall, ScheduledJob, ScrapeRun
from sales0.jobs import scheduled_discovery
from sales0.jobs.scheduled_discovery import DiscoveryStart, start_scheduled_discovery
from sales0.jobs.scheduler import (
    DigestJob, DiscoveryJob, JobDef, JobResult, PurgeJob, Scheduler, SheetSyncJob,
    register_purge_hook, unregister_purge_hook,
)
from sales0.services.sheetsync import InlineExecutor, InMemoryStateStore, SheetSyncRunner
from sales0.services.sheetsync.report import SyncReport

IST = timedelta(hours=5, minutes=30)


def ist(day: int, hh: int, mm: int = 0) -> datetime:
    """2026-10-<day> hh:mm Asia/Kolkata as UTC."""
    return datetime(2026, 10, day, hh, mm, tzinfo=UTC) - IST


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make(clock: Clock, seed: int = 1, jobs: list[JobDef] | None = None) -> Scheduler:
    """Discovery-only by default so the policy tests are not disturbed by the digest/purge jobs."""
    return Scheduler(sessions=get_sessionmaker(), now=clock, rng=random.Random(seed), jobs=jobs or [DiscoveryJob()])


def row(name: str) -> ScheduledJob:
    with session_scope() as db:
        r = db.scalar(select(ScheduledJob).where(ScheduledJob.name == name))
        assert r is not None
        db.expunge(r)
        return r


class Starts(list):
    """Records each discovery START; ``set`` chooses what the next ones return."""

    result = DiscoveryStart(True, run_id=7)

    def set(self, value: DiscoveryStart) -> None:
        self.result = value


@pytest.fixture
def starts(monkeypatch: pytest.MonkeyPatch) -> Starts:
    """Replace the discovery START only (policy tests)."""
    calls = Starts()

    def fake() -> DiscoveryStart:
        calls.append(1)
        return calls.result

    monkeypatch.setattr(scheduled_discovery, "start_scheduled_discovery", fake)
    return calls


# ---------------------------------------------------------------------- window math + jitter
def test_defaults_disabled_discovery_digest_and_purge_enabled(db_url: str) -> None:
    s = Scheduler(sessions=get_sessionmaker(), now=Clock(ist(28, 12)), rng=random.Random(1))  # the real default job set
    s.ensure_rows()
    assert (row("discovery_run").enabled, row("sheet_sync").enabled) == (False, False)
    assert (row("daily_digest").enabled, row("purge").enabled) == (True, True)
    assert row("discovery_run").next_run_at is None  # disabled -> nothing planned
    # today's 07:30 has passed and nothing ran today: planned as "due now" (runs once, no back-fill of earlier days)
    assert row("daily_digest").next_run_at == ist(28, 7, 30)


def test_digest_plans_today_when_time_not_yet_reached(db_url: str) -> None:
    s = Scheduler(sessions=get_sessionmaker(), now=Clock(ist(28, 6)), rng=random.Random(1))
    s.ensure_rows()
    assert row("daily_digest").next_run_at == ist(28, 7, 30)
    assert row("purge").next_run_at == ist(28, 4, 15)  # 04:15 already passed today and not done: due once


@pytest.mark.parametrize("seed", range(60))
def test_jitter_stays_inside_window_and_leaves_tail_margin(db_url: str, seed: int) -> None:
    clock = Clock(ist(28, 20))  # evening: tomorrow's window is planned
    s = make(clock, seed)
    s.set_enabled("discovery_run", True)
    nxt = row("discovery_run").next_run_at
    assert nxt is not None
    assert ist(29, 1) <= nxt <= ist(29, 4, 30), nxt  # window 01:00-05:00 minus the 30 min tail margin


def test_jitter_actually_varies(db_url: str) -> None:
    times = set()
    for seed in range(10):
        s = make(Clock(ist(28, 20)), seed)
        s.set_enabled("discovery_run", True)
        times.add(row("discovery_run").next_run_at)
        with session_scope() as db:
            db.query(ScheduledJob).delete()
    assert len(times) >= 8


def test_enabled_mid_window_plans_tonight_after_now_else_tomorrow(db_url: str) -> None:
    s = make(Clock(ist(28, 2)), 3)
    s.set_enabled("discovery_run", True)
    nxt = row("discovery_run").next_run_at
    assert ist(28, 2) <= nxt <= ist(28, 4, 30)
    with session_scope() as db:
        db.query(ScheduledJob).delete()
    s2 = make(Clock(ist(28, 4, 40)), 3)  # < 5 min of usable window left -> tomorrow
    s2.set_enabled("discovery_run", True)
    assert ist(29, 1) <= row("discovery_run").next_run_at <= ist(29, 4, 30)


def test_max_runs_per_day_is_min_of_setting_and_scrape_cap(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCHEDULER_DISCOVERY_MAX_RUNS_PER_DAY", "3")
    monkeypatch.setenv("SALES0_SCRAPE_MAX_RUNS_PER_DAY", "2")
    reset_settings()
    s = make(Clock(ist(28, 20)), 5)
    s.set_enabled("discovery_run", True)
    import json

    slots = json.loads(row("discovery_run").state_json)["slots"]
    assert len(slots) == 2  # bounded by the existing daily run cap
    ats = [datetime.fromisoformat(x["at"]) for x in slots]
    assert ats == sorted(ats) and all(ist(29, 1) <= a <= ist(29, 4, 30) for a in ats)


# ---------------------------------------------------------------------- firing / no overlap
def test_fires_once_at_the_slot_then_plans_tomorrow(db_url: str, starts: Starts) -> None:
    clock = Clock(ist(28, 20))
    s = make(clock, 2)
    s.set_enabled("discovery_run", True)
    first = row("discovery_run").next_run_at
    clock.now = first - timedelta(seconds=1)
    assert s.tick() == [] and starts == []
    clock.now = first
    assert s.tick() == ["discovery_run"] and len(starts) == 1
    r = row("discovery_run")
    assert (r.last_status, r.last_run_id, r.runs_total) == ("ok", 7, 1)
    assert r.next_run_at > first + timedelta(hours=12)  # tomorrow's window
    clock.now = first + timedelta(minutes=1)
    assert s.tick() == [] and len(starts) == 1  # no second start for the same slot


def test_busy_skip_is_recorded_and_retried_inside_window_never_double_started(db_url: str, starts: Starts) -> None:
    clock = Clock(ist(28, 20))
    s = make(clock, 2)
    s.set_enabled("discovery_run", True)
    starts.set(DiscoveryStart(False, code="run_in_progress", reason="A run is already in progress."))
    clock.now = row("discovery_run").next_run_at
    s.tick()
    r = row("discovery_run")
    assert r.last_status == "skipped" and "already in progress" in r.last_detail and r.last_run_id is None
    assert len(starts) == 1
    retry = r.next_run_at
    assert clock.now < retry <= clock.now + timedelta(minutes=11)
    clock.now = retry - timedelta(seconds=5)
    assert s.tick() == [] and len(starts) == 1  # waits for the retry time
    starts.set(DiscoveryStart(True, run_id=9))
    clock.now = retry
    s.tick()
    assert len(starts) == 2 and row("discovery_run").last_run_id == 9


def test_non_retryable_reason_skips_the_slot_and_records_reason(db_url: str, starts: Starts) -> None:
    clock = Clock(ist(28, 20))
    s = make(clock, 4)
    s.set_enabled("discovery_run", True)
    starts.set(DiscoveryStart(False, code="needs_attention", reason="LinkedIn needs attention: checkpoint."))
    clock.now = row("discovery_run").next_run_at
    s.tick()
    r = row("discovery_run")
    assert r.last_status == "skipped" and "needs attention" in r.last_detail
    assert len(starts) == 1 and r.next_run_at > clock.now + timedelta(hours=12)


def test_cooldown_retry_waits_for_cooldown_end_if_window_open(db_url: str, starts: Starts) -> None:
    clock = Clock(ist(28, 20))
    s = make(clock, 4)
    s.set_enabled("discovery_run", True)
    fire = row("discovery_run").next_run_at
    until = fire + timedelta(minutes=20)
    starts.set(DiscoveryStart(False, code="safety_cooldown", reason="cooling down", retry_at=until))
    clock.now = fire
    s.tick()
    if until < ist(29, 5):  # window still open after the cool-down
        assert row("discovery_run").next_run_at == until
    clock.now = fire + timedelta(minutes=5)
    s.tick()
    assert len(starts) == 1


# ---------------------------------------------------------------------- restart / missed-run policy
def test_restart_keeps_enabled_flag_plan_and_does_not_replan(db_url: str, starts: Starts) -> None:
    clock = Clock(ist(28, 20))
    s1 = make(clock, 11)
    s1.set_enabled("discovery_run", True)
    planned = row("discovery_run").next_run_at
    state = row("discovery_run").state_json
    s2 = make(clock, 99)  # "restart": new process, different RNG
    s2.ensure_rows()
    assert row("discovery_run").enabled is True
    assert row("discovery_run").next_run_at == planned and row("discovery_run").state_json == state
    clock.now = planned
    assert s2.tick() == ["discovery_run"]


def test_disable_persists_and_clears_next_run(db_url: str, starts: Starts) -> None:
    clock = Clock(ist(28, 20))
    s = make(clock)
    s.set_enabled("discovery_run", True)
    s.set_enabled("discovery_run", False)
    assert row("discovery_run").next_run_at is None and make(clock).snapshot()[0].enabled is False
    clock.now = ist(29, 3)
    assert make(clock).tick() == [] and starts == []


def test_slot_missed_while_down_inside_window_runs_once_late(db_url: str, starts: Starts) -> None:
    clock = Clock(ist(28, 20))
    s = make(clock, 6)
    s.set_enabled("discovery_run", True)
    planned = row("discovery_run").next_run_at
    clock.now = max(planned + timedelta(minutes=3), ist(29, 1, 10))  # app came back a bit late, window still open
    assert clock.now < ist(29, 5)
    assert make(clock).tick() == ["discovery_run"] and len(starts) == 1
    clock.now += timedelta(minutes=5)
    assert make(clock).tick() == [] and len(starts) == 1


def test_window_closed_while_down_is_recorded_missed_not_caught_up(db_url: str, starts: Starts) -> None:
    clock = Clock(ist(28, 20))
    s = make(clock, 6)
    s.set_enabled("discovery_run", True)
    clock.now = ist(29, 9)  # slept through the whole window
    assert make(clock, 8).tick() == []
    assert starts == []
    r = row("discovery_run")
    assert r.last_status == "missed" and "not caught up" in r.last_detail
    assert r.next_run_at > ist(29, 20) and r.next_run_at <= ist(30, 4, 30)  # tomorrow night's window
    import json

    assert json.loads(r.state_json)["missed"] == 1


def test_many_missed_days_never_produce_a_burst(db_url: str, starts: Starts) -> None:
    clock = Clock(ist(28, 20))
    s = make(clock, 6)
    s.set_enabled("discovery_run", True)
    clock.now = ist(31, 12)  # three days later
    for _ in range(5):
        make(clock, 8).tick()
    assert starts == []


# ---------------------------------------------------------------------- the REAL adapter (no fakes)
def _set_session(status: str, last_error: str | None = None) -> None:
    with session_scope() as db:
        r = db.get(LinkedInSession, 1)
        r.status, r.last_error = status, last_error


def _add_keyword() -> None:
    from sales0.services import keywords

    with session_scope() as db:
        keywords.add_keyword(db, "ai workshop")


def test_adapter_never_overlaps_an_active_run(db_url: str) -> None:
    _set_session("connected")
    _add_keyword()
    with session_scope() as db:
        db.add(ScrapeRun(trigger="manual", mode="live", status="running", started_at=datetime.now(UTC)))
    out = start_scheduled_discovery()
    assert out.started is False and out.code == "run_in_progress" and out.retryable
    with session_scope() as db:
        assert db.scalar(select(func.count(ScrapeRun.id))) == 1  # nothing was queued


def test_adapter_skips_with_reason_when_linkedin_needs_attention(db_url: str) -> None:
    _set_session("needs_attention", "LinkedIn showed a checkpoint")
    _add_keyword()
    out = start_scheduled_discovery()
    assert out.started is False and out.code == "needs_attention"
    assert "checkpoint" in out.reason and not out.retryable


def test_adapter_respects_daily_run_cap(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_MAX_RUNS_PER_DAY", "1")
    reset_settings()
    _set_session("connected")
    _add_keyword()
    with session_scope() as db:
        db.add(ScrapeRun(trigger="manual", mode="live", status="succeeded", started_at=datetime.now(UTC),
                         finished_at=datetime.now(UTC)))
    out = start_scheduled_discovery()
    assert (out.started, out.code) == (False, "daily_run_cap") and "Daily run limit" in out.reason


def test_adapter_refuses_during_safety_cooldown_with_retry_time(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "60")
    reset_settings()
    _set_session("connected")
    _add_keyword()
    now = datetime.now(UTC)
    with session_scope() as db:
        db.add(ScrapeRun(trigger="manual", mode="live", status="aborted", abort_reason="rate_limited",
                         started_at=now - timedelta(minutes=10), finished_at=now - timedelta(minutes=5)))
    out = start_scheduled_discovery()
    assert out.code == "safety_cooldown" and out.retryable and out.retry_at is not None and out.retry_at > now


def test_adapter_refuses_over_monthly_spend_cap(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_MONTHLY_SPEND_CAP_USD", "0.001")
    reset_settings()
    _set_session("connected")
    _add_keyword()
    with session_scope() as db:
        db.add(LlmCall(feature="triage", model="m", cost_usd_micros=5000, status="ok", created_at=datetime.now(UTC)))
    out = start_scheduled_discovery()
    assert (out.started, out.code) == (False, "spend_cap_reached")


def test_adapter_starts_a_run_through_the_real_service(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: the real start_run queues a row; the job body itself is stubbed out so no browser opens."""
    from sales0.jobs import discovery_job
    from sales0.jobs.runner import get_runner

    monkeypatch.setattr(discovery_job, "execute", lambda run_id: None)
    _set_session("connected")
    _add_keyword()
    out = start_scheduled_discovery()
    assert out.started and out.run_id
    get_runner().wait(10)
    with session_scope() as db:
        assert db.get(ScrapeRun, out.run_id).trigger == "manual"  # existing service; scheduler adds no new trigger kind


def test_run_now_records_skip_reason_and_does_not_touch_the_plan(db_url: str, starts: Starts) -> None:
    clock = Clock(ist(28, 20))
    s = make(clock, 2)
    s.set_enabled("discovery_run", True)
    planned = row("discovery_run").next_run_at
    starts.set(DiscoveryStart(False, code="needs_attention", reason="LinkedIn needs attention: x."))
    res = s.run_now("discovery_run")
    assert res.status == "skipped" and "needs attention" in res.detail
    assert row("discovery_run").next_run_at == planned
    starts.set(DiscoveryStart(True, run_id=12))
    assert s.run_now("discovery_run").run_id == 12
    assert row("discovery_run").next_run_at == planned  # manual run does not consume tonight's slot


# ---------------------------------------------------------------------- digest + purge
def test_digest_job_runs_once_per_local_day_and_stores_a_digest(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_DIGEST_AI_SUMMARY", "false")
    reset_settings()
    clock = Clock(ist(28, 6))
    s = make(clock, jobs=[DigestJob()])
    s.ensure_rows()
    clock.now = ist(28, 7, 30)
    assert s.tick() == ["daily_digest"]
    with session_scope() as db:
        d = db.scalar(select(Digest))
        assert d is not None and d.trigger == "scheduled" and str(d.digest_date) == "2026-10-28"
    clock.now = ist(28, 12)
    assert s.tick() == []
    assert row("daily_digest").next_run_at == ist(29, 7, 30)
    clock.now = ist(29, 7, 31)
    assert s.tick() == ["daily_digest"]
    with session_scope() as db:
        assert db.scalar(select(func.count(Digest.id))) == 2


def test_failing_job_backs_off_and_gives_up_for_the_day(db_url: str) -> None:
    class Boom(DigestJob):
        def _run(self, ctx, now):  # noqa: ANN001, ANN202
            raise RuntimeError("boom")

    clock = Clock(ist(28, 7, 30))
    s = make(clock, jobs=[Boom()])
    s.ensure_rows()
    fired = 0
    for _ in range(40):  # ticking every minute for 40 minutes must not storm
        fired += len(s.tick())
        clock.now += timedelta(minutes=1)
    assert fired == 3  # 07:30, 07:45, 08:00 (15 min back-off)
    assert row("daily_digest").last_status == "failed" and "RuntimeError" in row("daily_digest").last_detail
    for _ in range(300):
        fired += len(s.tick())
        clock.now += timedelta(minutes=1)
    assert fired == 4  # 4th failure, then it gives up until tomorrow
    assert row("daily_digest").next_run_at >= ist(29, 7, 30)


def test_purge_runs_registered_hooks_with_retention_cutoff(db_url: str) -> None:
    seen: list[datetime] = []
    register_purge_hook("probe", lambda db, cutoff: seen.append(cutoff) or 3)
    try:
        clock = Clock(ist(28, 4, 15))
        s = make(clock, jobs=[PurgeJob()])
        s.ensure_rows()
        old = datetime.now(UTC)
        with session_scope() as db:
            db.add(Digest(digest_date=datetime(2026, 1, 1).date(), sections_json="{}", created_at=ist(1, 1) - timedelta(days=200)))
            db.add(Digest(digest_date=datetime(2026, 10, 27).date(), sections_json="{}", created_at=ist(27, 8)))
        assert s.tick() == ["purge"]
        assert seen == [ist(28, 4, 15) - timedelta(days=90)]
        assert "probe=3" in row("purge").last_detail and "digests=1" in row("purge").last_detail
        with session_scope() as db:
            assert db.scalar(select(func.count(Digest.id))) == 1
        assert old
    finally:
        unregister_purge_hook("probe")


# ---------------------------------------------------------------------- sheet sync (existing runner)
def test_sheet_sync_job_delegates_to_the_existing_runner(db_url: str) -> None:
    clock = Clock(ist(28, 12))
    calls: list[int] = []

    def job() -> SyncReport:
        calls.append(1)
        return SyncReport(dry_run=False, started_at=clock(), finished_at=clock())

    def factory(settings):  # noqa: ANN001, ANN202
        return SheetSyncRunner(job, interval_min=30, jitter=0, first_run_delay_s=20, now=clock,
                               executor=InlineExecutor(), state=InMemoryStateStore())

    s = make(clock, jobs=[SheetSyncJob(runner_factory=factory)])
    s.ensure_rows()
    clock.now += timedelta(minutes=5)
    assert s.tick() == [] and calls == []  # disabled: the runner is never ticked
    s.set_enabled("sheet_sync", True)
    assert row("sheet_sync").next_run_at is not None
    s.tick()  # first tick builds the runner (first run ~20 s after construction)
    clock.now += timedelta(seconds=30)
    s.tick()
    assert calls == [1]
    r = row("sheet_sync")
    assert r.last_status == "ok" and r.last_started_at is not None
    clock.now += timedelta(minutes=10)
    s.tick()
    assert calls == [1]  # interval (30 min) respected
    clock.now += timedelta(minutes=25)
    s.tick()
    assert calls == [1, 1]
    assert s.run_now("sheet_sync").status == "ok" and calls == [1, 1, 1]


def test_sheet_sync_without_crm_repository_is_unavailable_with_a_reason(db_url: str) -> None:
    from sales0.services.sheetsync import register_repository_factory

    register_repository_factory(None)  # the app lifespan of an earlier test installs the CRM repository (process-global)
    s = make(Clock(ist(28, 12)), jobs=[SheetSyncJob()])
    view = s.snapshot()[0]
    assert view.note and "Phase 2" in view.note
    s.set_enabled("sheet_sync", True)
    assert row("sheet_sync").next_run_at is None
    assert s.run_now("sheet_sync").status == "skipped"


def test_snapshot_exposes_slots_and_last_result(db_url: str, starts: Starts) -> None:
    clock = Clock(ist(28, 20))
    s = make(clock, 2)
    s.set_enabled("discovery_run", True)
    views = {v.name: v for v in s.snapshot()}
    assert views["discovery_run"].extra["slots"][0]["status"] == "pending"
    assert views["discovery_run"].next_run_at == row("discovery_run").next_run_at
    assert JobResult("ok").detail == ""
