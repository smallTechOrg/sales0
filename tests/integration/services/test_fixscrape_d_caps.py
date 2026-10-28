"""Fixscrape D #8: per-account daily page-load cap, watch visit / run caps, page-load recording."""

from __future__ import annotations

import pytest

from sales0.clock import utcnow
from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import ScrapeRun, ScrapeRunKeyword, WatchRun
from sales0.jobs import discovery_job
from sales0.services import keywords, linkedin_accounts, runs
from tests.integration.jobs.helpers import FakeSearcher
from tests.integration.services.helpers import post, set_session_status
from tests.support.fake_llm import FakeLLMClient, triage_answer


@pytest.fixture(autouse=True)
def _env(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_KEYWORD_PAUSE_MIN_S", "0")
    monkeypatch.setenv("SALES0_SCRAPE_KEYWORD_PAUSE_MAX_S", "0")
    reset_settings()
    set_session_status("connected")


def add_run(*, page_loads: int = 0, watch: bool = False, account_id: int | None = None, visits: int = 0) -> int:
    with session_scope() as db:
        account_id = account_id or linkedin_accounts.default_account_id(db)
        run = ScrapeRun(
            trigger="manual", mode="live", status="succeeded", started_at=utcnow(), finished_at=utcnow(),
            page_loads=page_loads, linkedin_account_id=account_id,
        )
        db.add(run)
        db.flush()
        if watch:
            db.add(WatchRun(run_id=run.id, origin="manual"))
        for i in range(visits):
            db.add(ScrapeRunKeyword(run_id=run.id, keyword_text=f"watch:in/p{i}", status="done"))
        return run.id


def check(watch: bool = False, account_id: int | None = None):
    with session_scope() as db:
        error, _, _ = runs._check(db, utcnow(), account_id, watch=watch)
        return error


def add_kw(*texts: str) -> None:
    with session_scope() as db:
        for t in texts:
            keywords.add_keyword(db, t)


def test_new_conservative_defaults() -> None:
    from sales0.config import Settings

    class _D:  # the declared defaults (the test env overrides some of them)
        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return Settings.model_fields[name].default

    s = _D()
    assert (s.scrape_max_runs_per_day, s.scrape_max_passes_per_keyword, s.scrape_max_pageloads_per_day) == (2, 3, 600)
    assert (s.scrape_max_consecutive_errors, s.watch_max_visits_per_day, s.watch_max_runs_per_day) == (3, 30, 2)
    # arithmetic: a normal run = 40 keywords x 3 passes = 120 page loads, so 2 runs = 240 of 600; >= 200 posts/run
    # needs only ~2 cards per pass.
    assert 2 * s.scrape_max_keywords_per_run * s.scrape_max_passes_per_keyword <= s.scrape_max_pageloads_per_day
    assert s.scrape_max_keywords_per_run * s.scrape_max_passes_per_keyword * 2 >= 200


def test_pageload_cap_refuses_a_run_when_the_account_used_it_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_MAX_PAGELOADS_PER_DAY", "50")
    reset_settings()
    add_kw("ai workshop")
    add_run(page_loads=49)
    assert check() is None
    add_run(page_loads=1)
    err = check()
    assert err is not None and err.code == "daily_pageload_cap" and err.http_status == 429
    assert "50/50" in err.message


def test_pageload_cap_is_per_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_MAX_PAGELOADS_PER_DAY", "10")
    reset_settings()
    add_kw("ai workshop")
    with session_scope() as db:
        other = linkedin_accounts.add_account(db, "Second", backend="folder").id
        other_id = other
    add_run(page_loads=10)
    assert check() is not None
    set_session_status("connected")
    with session_scope() as db:
        row = linkedin_accounts.get_account(db, other_id)
        row.status = "connected"
    assert check(account_id=other_id) is None


def test_watch_visit_cap_and_page_loads_count_for_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_WATCH_MAX_VISITS_PER_DAY", "4")
    monkeypatch.setenv("SALES0_WATCH_MAX_RUNS_PER_DAY", "9")
    reset_settings()
    add_run(watch=True, visits=3)
    assert check(watch=True) is None
    add_run(watch=True, visits=1)
    err = check(watch=True)
    assert err is not None and err.code == "daily_visit_cap" and "4/4" in err.message


def test_watch_runs_have_their_own_daily_cap_and_do_not_use_the_discovery_allowance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_WATCH_MAX_RUNS_PER_DAY", "2")
    monkeypatch.setenv("SALES0_SCRAPE_MAX_RUNS_PER_DAY", "1")
    reset_settings()
    add_kw("ai workshop")
    add_run(watch=True)
    assert check() is None  # a watch run never uses the discovery allowance
    add_run(watch=True)
    err = check(watch=True)
    assert err is not None and err.code == "daily_watch_cap"
    assert check() is None


def test_watch_page_loads_count_in_the_shared_daily_total(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_MAX_PAGELOADS_PER_DAY", "5")
    reset_settings()
    add_kw("ai workshop")
    add_run(watch=True, page_loads=5, visits=5)
    err = check()
    assert err is not None and err.code == "daily_pageload_cap"


class CountingSearcher(FakeSearcher):
    """FakeSearcher with the slice-A ``page_loads`` property: 4 loads per scan."""

    page_loads = 0

    def scan(self, keyword: str, max_posts: int):  # type: ignore[no-untyped-def]
        self.page_loads += 4
        return super().scan(keyword, max_posts)


def queue_run() -> int:
    with session_scope() as db:
        run = ScrapeRun(trigger="manual", mode="fixture", status="queued", started_at=utcnow())
        db.add(run)
        db.flush()
        return run.id


def test_discovery_records_page_loads_and_stops_when_the_daily_budget_is_used_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_MAX_PAGELOADS_PER_DAY", "10")
    reset_settings()
    names = ["kw a", "kw b", "kw c", "kw d", "kw e"]
    add_kw(*names)
    searcher = CountingSearcher({n: [post(i, f"Company {i} wants a corporate AI trainer for a workshop")] for i, n in enumerate(names, 1)})
    rid = queue_run()
    discovery_job.execute(rid, llm=FakeLLMClient(responder=lambda req: triage_answer()), searcher_factory=searcher, sleep=lambda _s: None)
    with session_scope() as db:
        run = db.get(ScrapeRun, rid)
        assert run.page_loads == 12  # 3 scans x 4 loads: the 3rd crossed the budget of 10
        assert len(searcher.searched) == 3  # kw d / kw e never opened
        assert run.status == "succeeded" and run.abort_reason == "daily_post_cap"
        assert "page-load limit" in (run.current_step or "")


def test_discovery_passes_the_remaining_budget_to_the_searcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_MAX_PAGELOADS_PER_DAY", "100")
    reset_settings()
    add_kw("kw a")
    add_run(page_loads=30)
    seen: dict[str, object] = {}

    class Recorder(CountingSearcher):
        def __init__(self, settings, **kw):  # type: ignore[no-untyped-def]
            super().__init__({"kw a": []})
            seen.update(kw)

    monkeypatch.setattr("sales0.scraper.search.LinkedInSearcher", Recorder)
    rid = queue_run()
    discovery_job.execute(rid, llm=FakeLLMClient(responder=lambda req: triage_answer()), sleep=lambda _s: None)
    assert seen.get("page_load_budget") == 70
