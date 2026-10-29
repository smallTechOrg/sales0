"""Cost totals and run eligibility/recovery on real SQLite (timezone-aware day boundaries)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from sales0.db.engine import session_scope
from sales0.db.models import LlmCall, ScrapeRun, ScrapeRunKeyword
from sales0.errors import DomainError
from sales0.jobs.runner import get_runner
from sales0.services import costs, keywords, runs
from tests.integration.services.helpers import make_run, set_session_status

# 2026-10-27 10:00 IST == 04:30 UTC
NOW = datetime(2026, 10, 27, 4, 30, tzinfo=UTC)


def _call(at: datetime, micros: int) -> LlmCall:
    return LlmCall(feature="triage", model="m", input_tokens=1, output_tokens=1, cost_usd_micros=micros,
                   latency_ms=1, status="ok", created_at=at)


def test_cost_summary_splits_today_and_month_in_the_local_timezone(db_url: str) -> None:
    with session_scope() as db:
        db.add_all([
            _call(NOW, 1000),
            _call(datetime(2026, 10, 26, 19, 0, tzinfo=UTC), 300),  # 00:30 IST on the 27th -> today
            _call(datetime(2026, 10, 26, 18, 0, tzinfo=UTC), 50),  # 23:30 IST on the 26th -> month only
            _call(datetime(2026, 9, 30, 17, 0, tzinfo=UTC), 7),  # 22:30 IST Sep 30 -> neither
        ])
    with session_scope() as db:
        summary = costs.cost_summary(db, now=NOW)
    assert (summary.today_usd_micros, summary.today_calls) == (1300, 2)
    assert (summary.month_usd_micros, summary.month_calls) == (1350, 3)


def test_cost_summary_on_an_empty_db_is_zero(db_url: str) -> None:
    with session_scope() as db:
        s = costs.cost_summary(db)
    assert (s.today_usd_micros, s.month_usd_micros, s.today_calls, s.month_calls) == (0, 0, 0, 0)


def test_eligibility_reasons_in_order(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with session_scope() as db:
        e = runs.run_eligibility(db)
        assert (e.can_run, e.reason_code) == (False, "not_connected")
        assert e.reason == "Connect LinkedIn first (Setup → Connect LinkedIn)."
    set_session_status("needs_attention", "Checkpoint page at /checkpoint/challenge/")
    with session_scope() as db:
        e = runs.run_eligibility(db)
        assert e.reason_code == "needs_attention" and "Checkpoint page" in (e.reason or "")
    set_session_status("connected")
    with session_scope() as db:
        assert runs.run_eligibility(db).reason_code == "no_keywords"
        keywords.add_keyword(db, "ai workshop")
    with session_scope() as db:
        e = runs.run_eligibility(db)
        assert e.can_run and e.reason is None and e.runs_cap == 50


def test_daily_run_cap_and_post_cap_refuse_start(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from sales0.config import reset_settings

    monkeypatch.setenv("SALES0_SCRAPE_MAX_RUNS_PER_DAY", "1")
    monkeypatch.setenv("SALES0_SCRAPE_MAX_POSTS_PER_DAY", "5")
    reset_settings()
    set_session_status("connected")
    with session_scope() as db:
        keywords.add_keyword(db, "ai workshop")
        db.add(ScrapeRun(trigger="manual", mode="live", status="succeeded", started_at=datetime.now(UTC), posts_seen=2))
    with session_scope() as db:
        with pytest.raises(DomainError) as info:
            runs.start_run(db)
        assert info.value.code == "daily_run_cap" and info.value.http_status == 429
        assert "(1/1)" in info.value.message
    monkeypatch.setenv("SALES0_SCRAPE_MAX_RUNS_PER_DAY", "5")
    reset_settings()
    with session_scope() as db:
        run = db.scalars(select(ScrapeRun)).one()
        run.posts_seen = 5
    with session_scope() as db:
        e = runs.run_eligibility(db)
        assert (e.reason_code, e.posts_today, e.posts_cap) == ("daily_post_cap", 5, 5)


def test_yesterdays_runs_do_not_count_toward_today(db_url: str) -> None:
    with session_scope() as db:
        db.add(ScrapeRun(trigger="manual", mode="live", status="succeeded",
                         started_at=datetime.now(UTC) - timedelta(days=2), posts_seen=500))
    with session_scope() as db:
        e = runs.run_eligibility(db)
    assert (e.runs_today, e.posts_today) == (0, 0)


def test_run_in_progress_and_job_busy_are_refused(db_url: str) -> None:
    set_session_status("connected")
    with session_scope() as db:
        keywords.add_keyword(db, "ai workshop")
        db.add(ScrapeRun(trigger="manual", mode="live", status="running", started_at=datetime.now(UTC)))
    with session_scope() as db, pytest.raises(DomainError) as info:
        runs.start_run(db)
    assert info.value.code == "run_in_progress"
    with session_scope() as db:
        db.scalars(select(ScrapeRun)).one().status = "failed"
    import threading

    release = threading.Event()
    get_runner().submit("check", lambda: release.wait(5))
    try:
        with session_scope() as db, pytest.raises(DomainError) as busy:
            runs.start_run(db)
        assert busy.value.code == "job_busy"
    finally:
        release.set()
        get_runner().wait(5)


def test_recover_interrupted_runs_marks_queued_and_running_aborted_without_a_reason(db_url: str) -> None:
    queued, running, done = make_run("queued"), make_run("running"), make_run("succeeded")
    with session_scope() as db:
        db.add(ScrapeRunKeyword(run_id=running, keyword_text="ai workshop", status="running"))
    assert runs.recover_interrupted_runs_on_startup() == 2
    with session_scope() as db:
        for rid in (queued, running):
            detail = runs.get_run(db, rid)
            assert detail.run.status == "aborted" and detail.run.abort_reason is None
            assert detail.run.error_message == runs.INTERRUPTED_MESSAGE
            assert detail.run.error_message.startswith("Interrupted by server restart — the next run continues")
        assert runs.get_run(db, running).keywords[0].status == "skipped"
        assert runs.get_run(db, done).run.status == "succeeded"
        assert runs.active_run(db) is None
        assert runs.recover_interrupted_runs(db) == 0


def test_get_run_missing_raises_not_found_and_list_runs_is_newest_first(db_url: str) -> None:
    a, b = make_run(), make_run()
    with session_scope() as db:
        db.add(LlmCall(feature="triage", model="m", run_id=b, cost_usd_micros=900, status="ok"))
    with session_scope() as db:
        assert [r.id for r in runs.list_runs(db)] == [b, a]
        assert runs.list_runs(db)[0].cost_usd_micros == 900
        with pytest.raises(DomainError) as info:
            runs.get_run(db, 4242)
        assert info.value.code == "not_found"


def test_start_run_that_loses_the_runner_race_leaves_no_run_and_no_history(db_url: str,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    from sales0.db.models import StatusHistory
    from sales0.jobs.runner import job_busy_error

    set_session_status("connected")
    with session_scope() as db:
        keywords.add_keyword(db, "ai workshop")

    def lose_race(name: str, fn: object) -> None:
        raise job_busy_error("check")

    monkeypatch.setattr(get_runner(), "submit", lose_race)
    with session_scope() as db, pytest.raises(DomainError) as info:
        runs.start_run(db)
    assert info.value.code == "job_busy"
    with session_scope() as db:
        assert db.scalars(select(ScrapeRun)).all() == []
        assert db.scalars(select(StatusHistory).where(StatusHistory.entity_type == "scrape_run")).all() == []
