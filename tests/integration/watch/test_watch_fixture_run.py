"""Prospect watch against the fixture LinkedIn with the REAL ``LinkedInSearcher`` (headless Chromium)
and REAL Haiku triage. Fake profiles only; the real linkedin.com is never opened."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from sales0.clock import utcnow
from sales0.config import Settings, reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, LinkedinProfile, LlmCall, ScrapeRun, WatchRun
from sales0.jobs import watch_job
from sales0.linkedin_fixture import watch as fixture_watch
from sales0.services import events, runs, tracked_profiles
from sales0.services.events import TrackedProfileHit
from tests.integration.services.helpers import set_session_status
from tests.integration.watch.helpers import add_profile
from tests.support.fake_llm import FakeLLMClient, triage_answer

pytestmark = pytest.mark.browser


@pytest.fixture
def connected(db_url: str, connected_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("SALES0_WATCH_PROFILE_PAUSE_MIN_S", "0")
    monkeypatch.setenv("SALES0_WATCH_PROFILE_PAUSE_MAX_S", "0")
    reset_settings()
    set_session_status("connected")
    fixture_watch.reset_watch_posts()
    yield connected_settings
    fixture_watch.reset_watch_posts()


def run_watch(llm=None) -> int:
    with session_scope() as db:
        run = ScrapeRun(trigger="manual", mode="fixture", status="queued", started_at=utcnow())
        db.add(run)
        db.flush()
        db.add(WatchRun(run_id=run.id, origin="manual"))
        rid = run.id
    watch_job.execute(rid, llm=llm, sleep=lambda _s: None)
    return rid


def make_due() -> None:
    with session_scope() as db:
        db.execute(LinkedinProfile.__table__.update().values(last_checked_at=None))


@pytest.mark.llm
def test_activity_feeds_people_and_company_real_browser_real_haiku_then_idempotent(connected: Settings) -> None:
    asha = add_profile("in/watch-asha-rao", name="Asha Rao")
    bright = add_profile("company/watch-brightfield", name="Brightfield Pharma")
    quiet = add_profile("in/watch-quiet")
    hits: list[TrackedProfileHit] = []
    events.subscribe(TrackedProfileHit, lambda _db, e: hits.append(e))

    rid = run_watch()
    with session_scope() as db:
        run = runs.get_run(db, rid).run
        log = {r.slug: r for r in tracked_profiles.run_log(db, rid)}
        assert run.status == "succeeded"
        assert (log["in/watch-asha-rao"].posts_seen, log["company/watch-brightfield"].posts_seen) == (2, 1)
        assert log["in/watch-quiet"].posts_seen == 0
        assert log["in/watch-asha-rao"].posts_relevant == 1 and log["company/watch-brightfield"].posts_relevant == 1
        authors = {e.author_profile_id for e in db.scalars(select(Enquiry))}
        assert authors == {asha, bright}
        assert {h.profile_id for h in hits} == {asha, bright}
        assert db.get(LinkedinProfile, quiet).watch_last_status == "no_posts"
        llm_calls = db.scalar(select(func.count(LlmCall.id)))
        assert llm_calls and llm_calls >= 3

    # Idempotent: the same feeds again -> nothing new, no LLM call at all.
    make_due()
    hits.clear()
    rid2 = run_watch()
    with session_scope() as db:
        run2 = runs.get_run(db, rid2).run
        assert (run2.posts_seen, run2.posts_new, run2.posts_dupe, run2.posts_relevant) == (3, 0, 3, 0)
        assert db.scalar(select(func.count(LlmCall.id))) == llm_calls
        assert db.scalar(select(func.count(Enquiry.id))) == 2 and hits == []

    # A brand-new post appears: only it is triaged.
    fixture_watch.add_watch_post("watch-asha-rao", "Northwind Logistics also needs a corporate trainer for a 1-day "
                                 "leadership workshop for 20 managers in Pune in February. Send proposals to hr@northwind-logistics.example.")
    make_due()
    rid3 = run_watch()
    with session_scope() as db:
        run3 = runs.get_run(db, rid3).run
        assert (run3.posts_seen, run3.posts_new, run3.posts_dupe) == (4, 1, 3)
        assert db.scalar(select(func.count(LlmCall.id))) > llm_calls


def test_checkpoint_profile_stops_the_run_before_the_next_profile(connected: Settings) -> None:
    add_profile("in/watch-checkpoint-test")
    add_profile("in/watch-asha-rao", checked_days_ago=30)
    rid = run_watch(FakeLLMClient(responder=lambda req: triage_answer()))
    with session_scope() as db:
        run = runs.get_run(db, rid).run
        log = tracked_profiles.run_log(db, rid)
        assert (run.status, run.abort_reason) == ("aborted", "checkpoint")
        assert [(r.slug, r.status) for r in log] == [("in/watch-checkpoint-test", "error"), ("in/watch-asha-rao", "skipped")]
        assert db.scalar(select(func.count(Enquiry.id))) == 0


def test_unknown_profile_is_a_page_error_and_the_rest_continue(connected: Settings) -> None:
    add_profile("in/watch-unknown")
    add_profile("in/watch-asha-rao", checked_days_ago=30)
    rid = run_watch(FakeLLMClient.with_oracle(lambda t: "genuine" if "Generative AI" in t else "discard"))
    with session_scope() as db:
        log = {r.slug: r for r in tracked_profiles.run_log(db, rid)}
        assert log["in/watch-unknown"].status == "error" and "404" in (log["in/watch-unknown"].message or "")
        assert log["in/watch-asha-rao"].posts_seen == 2 and log["in/watch-asha-rao"].posts_relevant == 1


def test_company_and_person_feeds_parse_with_the_sdui_tier(connected: Settings) -> None:
    from sales0.scraper.search import LinkedInSearcher

    from sales0.scraper.profile_watch import scan_profile
    from sales0.config import get_settings

    with LinkedInSearcher(get_settings()) as s:
        person = scan_profile(s, "in/watch-asha-rao", 15)
        company = scan_profile(s, "company/watch-brightfield", 15)
        quiet = scan_profile(s, "in/watch-quiet", 15)
    assert (person.strategy, len(person.posts), person.drift_warning) == ("sdui", 2, False)
    assert (company.strategy, len(company.posts)) == ("sdui", 1)
    assert company.posts[0].author_profile_url.endswith("/company/watch-brightfield/")
    assert (len(quiet.posts), quiet.drift_warning, quiet.no_results) == (0, False, True)
