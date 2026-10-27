"""Discovery job control flow on real SQLite with a scripted searcher and FakeLLMClient.

Pins counters, dedupe, keyword order, per-keyword log, safety stops, daily cap, page errors
and fatal-LLM handling deterministically. The real-browser + real-Haiku runs live in
test_discovery_fixture_run.py.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from sales0.config import reset_settings
from sales0.db.engine import reset_engine, session_scope
from sales0.db.models import Enquiry, EnquirySource, Keyword, LinkedInSession, ScrapeRun
from sales0.jobs import discovery_job
from sales0.llm import LLMAuthError
from sales0.services import keywords, runs
from tests.integration.services.helpers import FakeSearcher, post, safety, scrape_error, set_session_status
from tests.support.fake_llm import FakeLLMClient, triage_answer

TEXT = {
    1: "Acme Logistics wants a training partner for a 2-day GenAI workshop in Pune",
    2: "SkillBridge Learning is hiring freelance Generative AI trainers for corporate clients",
    3: "Need an individual freelance trainer for an online prompt engineering batch, hourly rate",
    5: "Zentrix Retail wants an AI agents workshop for 30 engineers in Bengaluru",
    6: "Orbit Health wants a Build your first AI agent workshop, online, December",
    8: "Meridian Insurance needs a partner to design and build an AI assistant over SOPs",
    9: "I am a certified Generative AI trainer open to freelance assignments",
}
P7 = "SkillBridge learning is HIRING freelance Generative AI trainers\nfor corporate clients #hiring #trainers"


def corpus() -> dict[str, list]:
    return {
        "corporate ai training": [post(1, TEXT[1]), post(2, TEXT[2]), post(3, TEXT[3])],
        "ai workshop": [post(1, TEXT[1]), post(5, TEXT[5]), post(6, TEXT[6]), post(7, P7)],
        "ai consulting": [post(8, TEXT[8]), post(9, TEXT[9])],
    }


def genuine_llm() -> FakeLLMClient:
    return FakeLLMClient(responder=lambda req: triage_answer())


def setup(*texts: str) -> None:
    set_session_status("connected")
    with session_scope() as db:
        for t in texts:
            keywords.add_keyword(db, t)


def queue_run() -> int:
    with session_scope() as db:
        run = ScrapeRun(trigger="manual", mode="fixture", status="queued", started_at=datetime.now(UTC))
        db.add(run)
        db.flush()
        return run.id


def run_job(searcher: FakeSearcher, llm: FakeLLMClient | None = None) -> int:
    run_id = queue_run()
    discovery_job.execute(run_id, llm=llm or genuine_llm(), searcher_factory=searcher, sleep=lambda _s: None)
    return run_id


def detail(run_id: int):
    with session_scope() as db:
        return runs.get_run(db, run_id)


def test_run_one_counts_seen_new_dupes_and_triages_each_new_enquiry_once(db_url: str) -> None:
    setup("corporate ai training", "ai workshop", "ai consulting")
    llm = genuine_llm()
    rid = run_job(FakeSearcher(corpus()), llm)
    d = detail(rid)
    r = d.run
    assert r.status == "succeeded" and r.finished_at is not None and r.abort_reason is None
    assert (r.posts_seen, r.posts_new, r.posts_dupe, r.enquiries_triaged, r.triage_failed) == (9, 7, 2, 7, 0)
    assert (r.keywords_planned, r.keywords_done) == (3, 3)
    assert llm.calls == 7  # duplicates are never re-triaged
    assert [(k.keyword_text, k.status, k.posts_seen, k.posts_new, k.posts_dupe) for k in d.keywords] == [
        ("corporate ai training", "done", 3, 3, 0),
        ("ai workshop", "done", 4, 2, 2),
        ("ai consulting", "done", 2, 2, 0),
    ]
    assert all(k.duration_ms is not None for k in d.keywords)
    with session_scope() as db:
        assert db.scalar(select(func.count(Enquiry.id))) == 7
        assert all(k.last_run_at is not None for k in db.scalars(select(Keyword)))
        session = db.get(LinkedInSession, 1)
        assert session is not None and session.last_successful_run_at is not None
        p2 = db.scalar(select(EnquirySource.enquiry_id).where(EnquirySource.source_ref.endswith("02")))
        assert db.scalar(select(func.count(EnquirySource.id)).where(EnquirySource.enquiry_id == p2)) == 2


def test_run_two_searches_the_never_run_keyword_first_and_uses_fewshot(db_url: str) -> None:
    setup("corporate ai training", "ai workshop", "ai consulting")
    run_job(FakeSearcher(corpus()))
    from sales0.services import inbox

    with session_scope() as db:
        first = inbox.list_enquiries(db, "review", None).items
        inbox.decide(db, [first[0].id], "accept")
        inbox.decide(db, [first[1].id], "discard", "Not a requirement")
        keywords.add_keyword(db, "claude code training")
    data = corpus() | {"claude code training": [post(10, "QuantaWorks wants Claude Code training"),
                                                post(11, "Briltus is onboarding freelance trainers")]}
    searcher = FakeSearcher(data)
    llm = genuine_llm()
    rid = run_job(searcher, llm)
    assert searcher.searched[0] == "claude code training"
    r = detail(rid).run
    assert (r.posts_seen, r.posts_new, r.posts_dupe, r.enquiries_triaged) == (11, 2, 9, 2)
    assert llm.calls == 2 and all(req.log_context.get("fewshot_count", 0) >= 1 for req in llm.requests)


def test_checkpoint_aborts_marks_session_needs_attention_and_skips_the_rest(db_url: str) -> None:
    setup("checkpoint-test", "ai workshop")
    searcher = FakeSearcher({"checkpoint-test": safety("checkpoint"), "ai workshop": [post(5, TEXT[5])]})
    rid = run_job(searcher)
    d = detail(rid)
    assert d.run.status == "aborted" and d.run.abort_reason == "checkpoint"
    assert searcher.searched == ["checkpoint-test"]
    assert [k.status for k in d.keywords] == ["error", "skipped"]
    assert "checkpoint" in (d.run.error_message or "").lower()
    with session_scope() as db:
        s = db.get(LinkedInSession, 1)
        assert s is not None and s.status == "needs_attention"
        assert s.last_error and s.last_error.startswith("Checkpoint page at /")
        assert s.last_successful_run_at is None
        e = runs.run_eligibility(db)
        assert e.reason_code == "needs_attention"


def test_safety_stop_after_saving_posts_still_triages_them(db_url: str) -> None:
    setup("ai workshop", "captcha-test")
    llm = genuine_llm()
    rid = run_job(FakeSearcher({"ai workshop": [post(5, TEXT[5]), post(6, TEXT[6])], "captcha-test": safety("captcha")}), llm)
    r = detail(rid).run
    assert (r.status, r.abort_reason, r.posts_new, r.enquiries_triaged) == ("aborted", "captcha", 2, 2)


def test_logged_out_profile_aborts_with_logout_before_any_search(db_url: str) -> None:
    setup("ai workshop")
    searcher = FakeSearcher({}, enter_error=safety("logout"))
    d = detail(run_job(searcher))
    assert (d.run.status, d.run.abort_reason) == ("aborted", "logout")
    assert searcher.searched == [] and [k.status for k in d.keywords] == ["skipped"]


def test_page_error_marks_only_that_keyword_and_drift_is_a_warning(db_url: str) -> None:
    setup("ai workshop", "ai consulting", "corporate ai training")
    searcher = FakeSearcher({"ai workshop": scrape_error(), "ai consulting": [], "corporate ai training": [post(1, TEXT[1])]},
                            drift={"ai consulting"})
    d = detail(run_job(searcher))
    assert d.run.status == "succeeded" and d.run.errors_count == 1 and d.run.posts_new == 1
    by_kw = {k.keyword_text: k for k in d.keywords}
    assert by_kw["ai workshop"].status == "error" and "Page error" in (by_kw["ai workshop"].error_message or "")
    assert by_kw["ai consulting"].status == "warning"
    assert by_kw["ai consulting"].error_message == "No posts parsed — LinkedIn layout may have changed"
    assert by_kw["corporate ai training"].status == "done"


def test_fatal_llm_error_fails_the_run_keeps_enquiries_pending_and_stops_searching(db_url: str) -> None:
    setup("corporate ai training", "ai workshop")
    llm = FakeLLMClient(responder=lambda req: LLMAuthError("Anthropic rejected the key — check SALES0_ANTHROPIC_API_KEY in .env"))
    searcher = FakeSearcher(corpus())
    d = detail(run_job(searcher, llm))
    assert d.run.status == "failed" and "Anthropic rejected the key" in (d.run.error_message or "")
    assert d.run.posts_new == 3 and d.run.enquiries_triaged == 0
    assert searcher.searched == ["corporate ai training"]
    assert [k.status for k in d.keywords] == ["done", "skipped"]
    with session_scope() as db:
        statuses = set(db.scalars(select(Enquiry.triage_status)))
        assert statuses == {"pending"}


def test_daily_post_cap_ends_the_run_succeeded_with_remaining_keywords_skipped(db_url: str,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_MAX_POSTS_PER_DAY", "3")
    reset_settings()
    setup("corporate ai training", "ai workshop")
    d = detail(run_job(FakeSearcher(corpus())))
    assert (d.run.status, d.run.abort_reason, d.run.posts_seen) == ("succeeded", "daily_post_cap", 3)
    assert "Daily post limit reached (3/3)" in (d.run.current_step or "")
    assert [k.status for k in d.keywords] == ["done", "skipped"]


def test_per_keyword_post_cap_limits_seen(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_MAX_POSTS_PER_KEYWORD", "2")
    reset_settings()
    setup("corporate ai training", "ai workshop", "ai consulting")
    d = detail(run_job(FakeSearcher(corpus())))
    assert all(k.posts_seen <= 2 for k in d.keywords)


def test_inactive_keywords_are_skipped_and_keyword_cap_applies(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_MAX_KEYWORDS_PER_RUN", "1")
    reset_settings()
    setup("corporate ai training", "ai workshop")
    with session_scope() as db:
        kw = db.scalars(select(Keyword).where(Keyword.text_key == "corporate ai training")).one()
        kw.active = False
    searcher = FakeSearcher(corpus())
    d = detail(run_job(searcher))
    assert searcher.searched == ["ai workshop"] and d.run.keywords_planned == 1


def test_a_second_execute_on_a_finished_run_is_a_no_op(db_url: str) -> None:
    setup("ai workshop")
    rid = run_job(FakeSearcher(corpus()))
    searcher = FakeSearcher(corpus())
    discovery_job.execute(rid, llm=genuine_llm(), searcher_factory=searcher, sleep=lambda _s: None)
    assert searcher.searched == [] and detail(rid).run.status == "succeeded"


def test_finished_run_survives_an_engine_restart(db_url: str) -> None:
    setup("ai workshop")
    rid = run_job(FakeSearcher(corpus()))
    reset_engine()
    d = detail(rid)
    assert d.run.status == "succeeded" and d.run.posts_new == 4
    assert d.run.duration_ms is not None and d.run.duration_ms < int(timedelta(minutes=1).total_seconds() * 1000)


class _DeletesKeywordWhileSearching(FakeSearcher):
    """Deletes ``victim`` through the Setup service (own session) while that keyword is searched."""

    victim: str = ""

    def search(self, keyword: str, max_posts: int):  # type: ignore[override]
        if keyword == self.victim:
            with session_scope() as db:
                kid = db.scalar(select(Keyword.id).where(Keyword.text_key == keyword))
                assert kid is not None
                keywords.remove_keyword(db, kid)
        return super().search(keyword, max_posts)


def _deleting_searcher(data: dict, victim: str) -> _DeletesKeywordWhileSearching:
    searcher = _DeletesKeywordWhileSearching(data)
    searcher.victim = victim
    return searcher


def test_keyword_deleted_mid_search_with_zero_posts_does_not_crash_the_run(db_url: str) -> None:
    setup("ai consulting", "ai workshop")
    data = {"ai consulting": [], "ai workshop": [post(5, TEXT[5])]}
    searcher = _deleting_searcher(data, "ai consulting")
    llm = genuine_llm()
    d = detail(run_job(searcher, llm))
    assert d.run.status == "succeeded", d.run.error_message
    assert searcher.searched == ["ai consulting", "ai workshop"]
    assert (d.run.keywords_done, d.run.posts_seen, d.run.posts_new, d.run.enquiries_triaged, d.run.errors_count) == (
        2, 1, 1, 1, 0)
    by_kw = {k.keyword_text: k for k in d.keywords}
    assert by_kw["ai consulting"].status == "done" and by_kw["ai consulting"].posts_seen == 0
    with session_scope() as db:
        assert [k.text for k in db.scalars(select(Keyword))] == ["ai workshop"]
        assert db.scalars(select(Keyword)).one().last_run_at is not None


def test_keyword_deleted_mid_search_with_posts_keeps_and_triages_them(db_url: str) -> None:
    from sales0.db.models import EnquiryKeywordHit

    setup("corporate ai training", "ai workshop")
    searcher = _deleting_searcher(corpus(), "corporate ai training")
    llm = genuine_llm()
    rid = run_job(searcher, llm)
    d = detail(rid)
    assert d.run.status == "succeeded", d.run.error_message
    assert (d.run.posts_seen, d.run.posts_new, d.run.posts_dupe, d.run.enquiries_triaged, d.run.triage_failed) == (
        7, 5, 2, 5, 0)  # P1 is a URN dupe, P7 a content dupe of P2
    assert d.run.errors_count == 0 and llm.calls == 5
    by_kw = {k.keyword_text: k for k in d.keywords}
    deleted = by_kw["corporate ai training"]
    assert (deleted.status, deleted.posts_seen, deleted.posts_new, deleted.errors) == ("done", 3, 3, 0)
    with session_scope() as db:
        assert db.scalar(select(func.count(Enquiry.id))) == 5
        assert set(db.scalars(select(Enquiry.triage_status))) == {"done"}
        hits = db.scalars(
            select(EnquiryKeywordHit).where(EnquiryKeywordHit.run_id == rid,
                                            EnquiryKeywordHit.keyword_text == "corporate ai training")
        ).all()
        assert len(hits) == 3 and all(h.keyword_id is None for h in hits)


def test_fatal_llm_error_after_a_safety_stop_stays_aborted_but_shows_the_ai_error(
        db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Saved-but-untriaged enquiries at the moment of the stop: defer the in-loop triage once.
    real = discovery_job._Run.triage_pending
    calls = {"n": 0}

    def deferred_once(self: discovery_job._Run) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            real(self)

    monkeypatch.setattr(discovery_job._Run, "triage_pending", deferred_once)
    setup("ai workshop", "captcha-test")
    llm = FakeLLMClient(responder=lambda req: LLMAuthError("Anthropic rejected the key — check SALES0_ANTHROPIC_API_KEY in .env"))
    searcher = FakeSearcher({"ai workshop": [post(5, TEXT[5]), post(6, TEXT[6])], "captcha-test": safety("captcha")})
    d = detail(run_job(searcher, llm))
    assert (d.run.status, d.run.abort_reason, d.run.posts_new, d.run.enquiries_triaged) == ("aborted", "captcha", 2, 0)
    message = d.run.error_message or ""
    assert message.startswith("Run stopped: LinkedIn captcha detected.")
    assert "AI triage also stopped: Anthropic rejected the key" in message
    assert llm.calls == 1  # the fatal error stops triage at once
    with session_scope() as db:
        assert set(db.scalars(select(Enquiry.triage_status))) == {"pending"}


class _DeletesKeywordBetweenPosts(FakeSearcher):
    """Returns ``victim``'s posts as a list that deletes the keyword (Setup service, own
    session) right after the first post has been saved — i.e. inside the save loop, after
    the job resolved a live ``keyword_id``."""

    victim: str = ""

    def search(self, keyword: str, max_posts: int):  # type: ignore[override]
        result = super().search(keyword, max_posts)
        if keyword != self.victim:
            return result

        class _Posts(list):
            def __iter__(inner):  # noqa: N805
                for i, item in enumerate(list.__iter__(inner)):
                    if i == 1:
                        with session_scope() as db:
                            kid = db.scalar(select(Keyword.id).where(Keyword.text_key == keyword))
                            assert kid is not None
                            keywords.remove_keyword(db, kid)
                    yield item

        return dataclasses.replace(result, posts=_Posts(result.posts))


def test_keyword_deleted_between_posts_retries_the_post_without_the_keyword(db_url: str) -> None:
    from sales0.db.models import EnquiryKeywordHit

    setup("corporate ai training", "ai workshop")
    searcher = _DeletesKeywordBetweenPosts(corpus())
    searcher.victim = "corporate ai training"
    llm = genuine_llm()
    rid = run_job(searcher, llm)
    d = detail(rid)
    assert d.run.status == "succeeded", d.run.error_message
    # No post is lost to the FK race: same totals as an undisturbed run of these two keywords.
    assert (d.run.posts_seen, d.run.posts_new, d.run.posts_dupe, d.run.enquiries_triaged) == (7, 5, 2, 5)
    assert d.run.errors_count == 0 and llm.calls == 5
    by_kw = {k.keyword_text: k for k in d.keywords}
    deleted = by_kw["corporate ai training"]
    assert (deleted.status, deleted.posts_seen, deleted.posts_new, deleted.errors) == ("done", 3, 3, 0)
    with session_scope() as db:
        assert db.scalar(select(func.count(Enquiry.id))) == 5
        hits = db.scalars(
            select(EnquiryKeywordHit)
            .where(EnquiryKeywordHit.run_id == rid, EnquiryKeywordHit.keyword_text == "corporate ai training")
            .order_by(EnquiryKeywordHit.id)
        ).all()
        # The first hit was saved with the live id and then SET NULL by the delete; the
        # second and third were retried / saved with keyword_id NULL. All keep the text.
        assert len(hits) == 3 and all(h.keyword_id is None for h in hits)
        assert [k.text for k in db.scalars(select(Keyword))] == ["ai workshop"]
