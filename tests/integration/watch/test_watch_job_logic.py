"""Prospect-watch run control flow on real SQLite with a scripted searcher and FakeLLMClient:
counters, the store-only-if-relevant pipeline, TrackedProfileHit, idempotency, caps, safety stops."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from sales0.clock import utcnow
from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, EnquiryKeywordHit, LinkedinProfile, LlmCall, ScrapeRun, SeenPost, WatchRun
from sales0.jobs import watch_job
from sales0.jobs.runner import get_runner
from sales0.llm import set_llm_client
from sales0.services import events, runs, tracked_profiles
from sales0.services.events import TrackedProfileHit
from tests.integration.services.helpers import safety, scrape_error, session_missing, set_session_status
from tests.integration.watch.helpers import WatchSearcher, add_profile, profile_post
from tests.support.fake_llm import FakeLLMClient

REQ = "Northwind Logistics wants a corporate trainer for a 2-day Generative AI workshop in Mumbai, 30 managers, December."
CHATTER = "So proud of our warehouse team for winning the regional safety award this quarter!"


def oracle() -> FakeLLMClient:
    return FakeLLMClient.with_oracle(lambda text: "discard" if "safety award" in text else "genuine")


def queue(origin: str = "manual") -> int:
    with session_scope() as db:
        run = ScrapeRun(trigger="manual", mode="fixture", status="queued", started_at=utcnow())
        db.add(run)
        db.flush()
        db.add(WatchRun(run_id=run.id, origin=origin))
        return run.id


def run_job(searcher: WatchSearcher, llm: FakeLLMClient | None = None) -> int:
    rid = queue()
    watch_job.execute(rid, llm=llm or oracle(), searcher_factory=searcher, sleep=lambda _s: None)
    return rid


def detail(rid: int):
    with session_scope() as db:
        return runs.get_run(db, rid), tracked_profiles.run_log(db, rid)


@pytest.fixture(autouse=True)
def _env(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_WATCH_PROFILE_PAUSE_MIN_S", "0")
    monkeypatch.setenv("SALES0_WATCH_PROFILE_PAUSE_MAX_S", "0")
    reset_settings()
    set_session_status("connected")


def test_relevant_new_post_becomes_an_enquiry_linked_to_the_profile_and_publishes_a_hit() -> None:
    pid = add_profile("in/asha-rao")
    other = add_profile("in/quiet-one")
    hits: list[TrackedProfileHit] = []
    events.subscribe(TrackedProfileHit, lambda _db, e: hits.append(e))
    searcher = WatchSearcher({"in/asha-rao": [profile_post("in/asha-rao", "k1", REQ), profile_post("in/asha-rao", "k2", CHATTER)]})
    llm = oracle()
    rid = run_job(searcher, llm)
    d, log = detail(rid)
    assert d.run.status == "succeeded"
    assert (d.run.posts_seen, d.run.posts_new, d.run.posts_relevant, d.run.posts_skipped_ai) == (2, 2, 1, 1)
    assert (d.run.keywords_planned, d.run.keywords_done) == (2, 2)
    assert llm.calls == 2
    by = {r.slug: r for r in log}
    assert (by["in/asha-rao"].status, by["in/asha-rao"].posts_seen, by["in/asha-rao"].posts_new, by["in/asha-rao"].posts_relevant) == ("done", 2, 2, 1)
    assert by["in/quiet-one"].posts_seen == 0 and by["in/quiet-one"].message == "No posts on the page"
    with session_scope() as db:
        enq = db.scalars(select(Enquiry)).all()
        assert len(enq) == 1 and enq[0].author_profile_id == pid and enq[0].source == "linkedin_post"
        assert db.scalar(select(EnquiryKeywordHit.keyword_text).where(EnquiryKeywordHit.enquiry_id == enq[0].id)) == "watch:in/asha-rao"
        row = db.get(LinkedinProfile, pid)
        assert (row.watch_checks, row.watch_hits, row.watch_last_status) == (1, 1, "ok") and row.last_checked_at is not None
        assert db.get(LinkedinProfile, other).watch_last_status == "no_posts"
        assert db.get(WatchRun, rid).hits == 1
        assert hits == [TrackedProfileHit(profile_id=pid, enquiry_id=enq[0].id, run_id=rid)]
        assert db.scalar(select(func.count(SeenPost.id))) == 2


def test_second_watch_run_is_idempotent_zero_new_zero_llm_calls() -> None:
    add_profile("in/asha-rao")
    posts = {"in/asha-rao": [profile_post("in/asha-rao", "k1", REQ), profile_post("in/asha-rao", "k2", CHATTER)]}
    run_job(WatchSearcher(posts))
    with session_scope() as db:  # make it due again
        db.execute(LinkedinProfile.__table__.update().values(last_checked_at=utcnow() - timedelta(days=4)))
    llm = oracle()
    hits: list[TrackedProfileHit] = []
    events.subscribe(TrackedProfileHit, lambda _db, e: hits.append(e))
    rid = run_job(WatchSearcher(posts), llm)
    d, _ = detail(rid)
    assert (d.run.posts_seen, d.run.posts_new, d.run.posts_dupe, d.run.posts_relevant) == (2, 0, 2, 0)
    assert llm.calls == 0 and hits == []
    with session_scope() as db:
        assert db.scalar(select(func.count(Enquiry.id))) == 1
        assert db.scalar(select(func.count(LlmCall.id))) == 2  # only the first run's two calls


def test_a_new_post_on_the_next_visit_is_the_only_one_triaged() -> None:
    add_profile("in/asha-rao")
    run_job(WatchSearcher({"in/asha-rao": [profile_post("in/asha-rao", "k1", REQ)]}))
    with session_scope() as db:
        db.execute(LinkedinProfile.__table__.update().values(last_checked_at=utcnow() - timedelta(days=4)))
    llm = oracle()
    newer = "Brightfield Pharma needs a data analytics trainer for 25 analysts in Hyderabad in January, in person."
    rid = run_job(WatchSearcher({"in/asha-rao": [profile_post("in/asha-rao", "k9", newer), profile_post("in/asha-rao", "k1", REQ)]}), llm)
    d, _ = detail(rid)
    assert (d.run.posts_seen, d.run.posts_new, d.run.posts_dupe, d.run.posts_relevant) == (2, 1, 1, 1) and llm.calls == 1


def test_only_tracked_unpaused_due_profiles_are_visited_least_recent_first_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_WATCH_MAX_PROFILES_PER_RUN", "3")
    reset_settings()
    add_profile("in/never-checked")
    add_profile("in/old", checked_days_ago=30)
    add_profile("in/older", checked_days_ago=60)
    add_profile("in/recent", checked_days_ago=1)  # < 3 days: not due
    add_profile("in/paused", paused=True)
    add_profile("in/untracked", tracked=False)
    add_profile("in/never-tracked", tracked=None)
    add_profile("in/extra", checked_days_ago=20)  # due, but beyond the cap of 3
    searcher = WatchSearcher({})
    rid = run_job(searcher)
    assert searcher.visited == ["in/never-checked", "in/older", "in/old"]
    d, log = detail(rid)
    assert d.run.keywords_planned == 3 and [r.slug for r in log] == searcher.visited
    with session_scope() as db:
        for slug in ("in/untracked", "in/never-tracked", "in/paused", "in/recent"):
            assert db.scalar(select(LinkedinProfile.watch_checks).where(LinkedinProfile.slug == slug)) == 0


def test_a_profile_untracked_after_planning_is_not_visited() -> None:
    add_profile("in/a")
    b = add_profile("in/b")

    def on_scan(slug: str) -> None:
        if slug == "in/a":
            with session_scope() as db:
                tracked_profiles.untrack(db, b)

    searcher = WatchSearcher({}, on_scan=on_scan)
    rid = run_job(searcher)
    assert searcher.visited == ["in/a"]
    _, log = detail(rid)
    assert [(r.slug, r.status, r.message) for r in log] == [("in/a", "done", "No posts on the page"), ("in/b", "skipped", "No longer tracked")]


def test_max_posts_per_profile_and_the_daily_post_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_WATCH_MAX_POSTS_PER_PROFILE", "2")
    monkeypatch.setenv("SALES0_SCRAPE_MAX_POSTS_PER_DAY", "3")
    reset_settings()
    for s in ("in/a", "in/b", "in/c"):
        add_profile(s)
    mk = lambda s: [profile_post(s, f"{s}{i}", f"{REQ} variant {s} {i}") for i in range(5)]  # noqa: E731
    searcher = WatchSearcher({s: mk(s) for s in ("in/a", "in/b", "in/c")})
    rid = run_job(searcher)
    d, log = detail(rid)
    assert searcher.max_posts_seen[0] == 2
    assert d.run.posts_seen == 3 and d.run.status == "succeeded" and d.run.abort_reason == "daily_post_cap"
    assert searcher.visited == ["in/a", "in/b"]  # b got the 1 remaining slot; c was not visited
    assert [r.status for r in log] == ["done", "done", "skipped"]
    with session_scope() as db:
        assert tracked_profiles.watch_eligibility(db)[0].code == "daily_post_cap"


@pytest.mark.parametrize("kind", ["checkpoint", "captcha", "rate_limited", "logout"])
def test_safety_stop_aborts_the_run_flags_the_session_and_starts_the_shared_cooldown(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "30")
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN", "30")
    reset_settings()
    add_profile("in/a")
    add_profile("in/b", checked_days_ago=40)
    # in/a is least recently checked (never) -> visited first
    searcher = WatchSearcher({"in/a": safety(kind)})
    rid = run_job(searcher)
    d, log = detail(rid)
    assert (d.run.status, d.run.abort_reason) == ("aborted", kind)
    assert searcher.visited == ["in/a"] and [r.status for r in log] == ["error", "skipped"]
    with session_scope() as db:
        assert db.get(LinkedinProfile, 1).watch_last_status == "stopped" and db.get(LinkedinProfile, 1).watch_checks == 0
        assert db.get(LinkedinProfile, 1).last_checked_at is None  # a stop is not a check
        err = tracked_profiles.watch_eligibility(db)[0]
        assert err is not None and err.code == "needs_attention"
        set_session_status("connected")
        assert runs.run_eligibility(db).reason_code == "safety_cooldown"  # discovery obeys the same cool-down
        assert tracked_profiles.watch_eligibility(db)[0].code == "safety_cooldown"


def test_page_error_on_one_profile_does_not_stop_the_others() -> None:
    add_profile("in/a")
    add_profile("in/b", checked_days_ago=10)
    searcher = WatchSearcher({"in/a": scrape_error("LinkedIn returned HTTP 404"), "in/b": [profile_post("in/b", "kb", REQ)]})
    rid = run_job(searcher)
    d, log = detail(rid)
    assert d.run.status == "succeeded" and d.run.errors_count == 1
    assert [(r.slug, r.status) for r in log] == [("in/a", "error"), ("in/b", "done")]
    with session_scope() as db:
        assert db.scalar(select(LinkedinProfile.watch_last_status).where(LinkedinProfile.slug == "in/a")) == "error"


def test_drift_and_foreign_posts_are_reported_not_hidden() -> None:
    add_profile("in/a")
    add_profile("in/b")
    other = profile_post("in/b", "kx", REQ, author_url="https://www.linkedin.com/in/someone-else/")
    searcher = WatchSearcher({"in/a": [], "in/b": [other, profile_post("in/b", "ky", CHATTER)]}, drift={"in/a"})
    rid = run_job(searcher)
    _, log = detail(rid)
    by = {r.slug: r for r in log}
    assert by["in/a"].status == "warning" and "layout may have changed" in (by["in/a"].message or "")
    assert by["in/b"].posts_seen == 1 and "1 post by others skipped" in (by["in/b"].message or "")


def test_session_missing_fails_the_run_without_a_cooldown() -> None:
    add_profile("in/a")
    rid = run_job(WatchSearcher({}, enter_error=session_missing()))
    d, log = detail(rid)
    assert (d.run.status, d.run.abort_reason) == ("failed", None) and [r.status for r in log] == ["skipped"]


def test_no_due_profile_finishes_cleanly_and_start_is_refused() -> None:
    add_profile("in/a", checked_days_ago=1)
    with session_scope() as db:
        err, due = tracked_profiles.watch_eligibility(db)
        assert due == 0 and err is not None and err.code == "nothing_due"


def test_watch_runs_do_not_use_up_the_daily_run_allowance_or_plan_keywords(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_MAX_RUNS_PER_DAY", "1")
    reset_settings()
    add_profile("in/a")
    run_job(WatchSearcher({}))
    with session_scope() as db:
        el = runs.run_eligibility(db)
        assert el.runs_today == 0
        assert db.scalar(select(func.count(ScrapeRun.id))) == 1


def test_start_watch_run_goes_through_the_single_browser_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    add_profile("in/a")
    set_llm_client(oracle())
    searcher = WatchSearcher({"in/a": [profile_post("in/a", "k1", REQ)]})
    monkeypatch.setattr(watch_job, "_default_factory", lambda _s: searcher)
    monkeypatch.setattr(watch_job.time, "sleep", lambda _s: None)
    with session_scope() as db:
        view = tracked_profiles.start_watch_run(db)
        assert view.status == "queued"
    assert get_runner().wait(60)
    d, _ = detail(view.id)
    assert d.run.status == "succeeded" and d.run.posts_relevant == 1
    with session_scope() as db:
        assert tracked_profiles.is_watch_run(db, view.id)
        assert tracked_profiles.latest_watch_run(db).id == view.id


def test_watch_and_discovery_never_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    from sales0.errors import DomainError
    from sales0.services import keywords

    add_profile("in/a")
    with session_scope() as db:
        keywords.add_keyword(db, "ai workshop")
        db.add(ScrapeRun(trigger="manual", mode="fixture", status="running", started_at=utcnow()))
    with session_scope() as db:
        with pytest.raises(DomainError) as exc:
            tracked_profiles.start_watch_run(db)
        assert exc.value.code == "run_in_progress"
    with session_scope() as db:
        db.execute(ScrapeRun.__table__.update().values(status="succeeded", finished_at=utcnow()))
    with session_scope() as db:
        tracked_profiles.start_watch_run(db)  # queued; the runner thread now holds the slot
        with pytest.raises(DomainError) as exc:
            runs.start_run(db)
        assert exc.value.code in ("run_in_progress", "job_busy")
    get_runner().wait(60)


def test_untrack_track_pause_and_the_track_author_button() -> None:
    pid = add_profile("in/a", tracked=None)
    with session_scope() as db:
        assert tracked_profiles.list_tracked(db) == []
        tracked_profiles.track(db, pid)
        assert [t.slug for t in tracked_profiles.list_tracked(db)] == ["in/a"]
        tracked_profiles.set_paused(db, pid, True)
        assert tracked_profiles.list_tracked(db)[0].paused and tracked_profiles.due_count(db) == 0
        tracked_profiles.set_paused(db, pid, False)
        assert tracked_profiles.due_count(db) == 1
        tracked_profiles.untrack(db, pid)
        assert tracked_profiles.list_tracked(db) == [] and tracked_profiles.due_count(db) == 0
        assert db.get(LinkedinProfile, pid).tracked is False
