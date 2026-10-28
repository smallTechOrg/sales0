"""Fixscrape D #3 / #8 for the watch job: consecutive-error breaker, visit cap and page-load recording."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from sales0.clock import utcnow
from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import ScrapeRun, WatchRun
from sales0.jobs import watch_job
from sales0.services import runs
from tests.integration.services.helpers import scrape_error, set_session_status
from tests.integration.watch.helpers import WatchSearcher, add_profile, profile_post
from tests.support.fake_llm import FakeLLMClient

REQ = "Northwind Logistics wants a corporate trainer for a 2-day Generative AI workshop in Mumbai, 30 managers."


@pytest.fixture(autouse=True)
def _env(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_WATCH_PROFILE_PAUSE_MIN_S", "0")
    monkeypatch.setenv("SALES0_WATCH_PROFILE_PAUSE_MAX_S", "0")
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "60")
    reset_settings()
    set_session_status("connected")


def run_job(searcher: WatchSearcher) -> int:
    with session_scope() as db:
        run = ScrapeRun(trigger="manual", mode="fixture", status="queued", started_at=utcnow())
        db.add(run)
        db.flush()
        db.add(WatchRun(run_id=run.id, origin="manual"))
        rid = run.id
    watch_job.execute(rid, llm=FakeLLMClient.with_oracle(lambda t: "genuine"), searcher_factory=searcher, sleep=lambda _s: None)
    return rid


def test_three_consecutive_profile_errors_stop_the_run_as_rate_limited_with_a_cooldown() -> None:
    slugs = [f"in/person-{i}" for i in range(6)]
    for s in slugs:
        add_profile(s)
    searcher = WatchSearcher({s: scrape_error("timeout loading profile") for s in slugs})
    rid = run_job(searcher)
    assert len(searcher.visited) == 3  # the 4th..6th are never opened
    with session_scope() as db:
        run = db.get(ScrapeRun, rid)
        assert (run.status, run.abort_reason) == ("aborted", "rate_limited")
        assert run.page_loads == 3
        assert runs.safety_cooldown_until(db) is not None
    with session_scope() as db:
        from sales0.services import session as session_service

        assert session_service.session_status(db).stored_status == "needs_attention"


def test_a_success_resets_the_consecutive_error_count() -> None:
    slugs = [f"in/person-{i}" for i in range(5)]
    for s in slugs:
        add_profile(s)
    results = {s: scrape_error("timeout") for s in slugs}
    # every profile is due; the order is stable by id: errors on 0,1 then success on 2, errors 3,4 -> no breaker
    results[slugs[2]] = [profile_post(slugs[2], "ok", REQ)]
    searcher = WatchSearcher(results)
    rid = run_job(searcher)
    assert len(searcher.visited) == 5
    with session_scope() as db:
        run = db.get(ScrapeRun, rid)
        assert run.status == "succeeded" and run.abort_reason is None
        assert run.page_loads == 5  # one page load per visit


def test_the_breaker_threshold_is_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_MAX_CONSECUTIVE_ERRORS", "1")
    reset_settings()
    for i in range(3):
        add_profile(f"in/p{i}")
    searcher = WatchSearcher({f"in/p{i}": scrape_error() for i in range(3)})
    rid = run_job(searcher)
    assert len(searcher.visited) == 1
    with session_scope() as db:
        assert db.get(ScrapeRun, rid).abort_reason == "rate_limited"


def test_visit_cap_ends_the_run_and_records_which_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_WATCH_MAX_VISITS_PER_DAY", "2")
    reset_settings()
    slugs = [f"in/v{i}" for i in range(5)]
    for s in slugs:
        add_profile(s)
    searcher = WatchSearcher({s: [] for s in slugs})
    rid = run_job(searcher)
    assert len(searcher.visited) == 2
    with session_scope() as db:
        run = db.get(ScrapeRun, rid)
        assert run.abort_reason == "daily_post_cap" and "profile-visit limit" in (run.current_step or "")
        err, _, _ = runs._check(db, utcnow(), watch=True)
        assert err is not None and err.code == "daily_visit_cap"


def test_page_load_cap_ends_the_watch_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_MAX_PAGELOADS_PER_DAY", "2")
    reset_settings()
    for i in range(5):
        add_profile(f"in/q{i}")
    searcher = WatchSearcher({f"in/q{i}": [] for i in range(5)})
    rid = run_job(searcher)
    assert len(searcher.visited) == 2
    with session_scope() as db:
        run = db.get(ScrapeRun, rid)
        assert run.page_loads == 2 and "page-load limit" in (run.current_step or "")
        assert db.scalar(select(ScrapeRun.abort_reason).where(ScrapeRun.id == rid)) == "daily_post_cap"
