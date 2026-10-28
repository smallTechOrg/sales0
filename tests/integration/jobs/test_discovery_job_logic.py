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
from structlog.testing import capture_logs

from sales0.services.session import get_session_row
from sales0.config import reset_settings
from sales0.db.engine import reset_engine, session_scope
from sales0.db.models import Enquiry, EnquirySource, Keyword, LinkedinProfile, LlmCall, ScrapeRun, SeenPost
from sales0.jobs import discovery_job
from sales0.llm import LLMAuthError
from sales0.services import keywords, runs
from tests.integration.jobs.helpers import FakeSearcher, permalink_for
from tests.integration.services.helpers import (
    post,
    sdui_post,
    safety,
    scrape_error,
    session_missing,
    set_session_status,
)
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
    assert (r.posts_relevant, r.posts_skipped_ai) == (7, 0)
    assert (r.current_step or "").startswith("Run finished — scanned 9 · new 7 · relevant 7 (100%)")
    assert [(k.keyword_text, k.status, k.posts_seen, k.posts_new, k.posts_dupe) for k in d.keywords] == [
        ("corporate ai training", "done", 3, 3, 0),
        ("ai workshop", "done", 4, 2, 2),
        ("ai consulting", "done", 2, 2, 0),
    ]
    assert all(k.duration_ms is not None for k in d.keywords)
    with session_scope() as db:
        assert db.scalar(select(func.count(Enquiry.id))) == 7
        assert db.scalar(select(func.count(SeenPost.id))) == 7  # every distinct post is remembered once
        assert all(k.last_run_at is not None for k in db.scalars(select(Keyword)))
        session = get_session_row(db)
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
        s = get_session_row(db)
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
                            drift={"ai consulting"}, extra={"ai consulting": {"cards_found": 6}})
    d = detail(run_job(searcher))
    assert d.run.status == "succeeded" and d.run.errors_count == 1 and d.run.posts_new == 1
    by_kw = {k.keyword_text: k for k in d.keywords}
    assert by_kw["ai workshop"].status == "error" and "Page error" in (by_kw["ai workshop"].error_message or "")
    assert by_kw["ai consulting"].status == "warning"
    assert by_kw["ai consulting"].error_message == (
        "No posts parsed — LinkedIn layout may have changed (6 result cards found)")
    assert by_kw["corporate ai training"].status == "done"
    assert d.run.layout_warnings == 1


def test_fatal_llm_error_fails_the_run_leaves_posts_pending_and_stops_searching(db_url: str) -> None:
    setup("corporate ai training", "ai workshop")
    llm = FakeLLMClient(responder=lambda req: LLMAuthError("Anthropic rejected the key — check SALES0_ANTHROPIC_API_KEY in .env"))
    searcher = FakeSearcher(corpus())
    d = detail(run_job(searcher, llm))
    assert d.run.status == "failed" and "Anthropic rejected the key" in (d.run.error_message or "")
    assert d.run.posts_new == 3 and d.run.enquiries_triaged == 0 and d.run.posts_relevant == 0
    assert searcher.searched == ["corporate ai training"]
    assert [k.status for k in d.keywords] == ["done", "skipped"]
    with session_scope() as db:
        assert db.scalar(select(func.count(Enquiry.id))) == 0
        assert set(db.scalars(select(SeenPost.triage_status))) == {"pending"}  # retried by the next run
        assert db.scalar(select(func.count(LlmCall.id)).where(LlmCall.status == "error")) >= 1


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

    def scan(self, keyword: str, max_posts: int):  # type: ignore[override]
        if keyword == self.victim:
            with session_scope() as db:
                kid = db.scalar(select(Keyword.id).where(Keyword.text_key == keyword))
                assert kid is not None
                keywords.remove_keyword(db, kid)
        return super().scan(keyword, max_posts)


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


class _DeletesKeywordBetweenPosts(FakeSearcher):
    """Returns ``victim``'s posts as a list that deletes the keyword (Setup service, own
    session) right after the first post has been saved — i.e. inside the save loop, after
    the job resolved a live ``keyword_id``."""

    victim: str = ""

    def scan(self, keyword: str, max_posts: int):  # type: ignore[override]
        result = super().scan(keyword, max_posts)
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


# ---------------------------------------------------------------------- Phase 1.1
NOT_LOGGED_IN = "Not logged in to LinkedIn in sales0's browser — press Connect LinkedIn in Setup"


def test_no_session_cookie_at_run_start_fails_the_run_and_sets_not_connected(db_url: str,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "60")
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN", "15")
    reset_settings()
    setup("ai workshop", "ai consulting")
    searcher = FakeSearcher({}, enter_error=session_missing())
    llm = genuine_llm()
    d = detail(run_job(searcher, llm))
    assert (d.run.status, d.run.abort_reason, d.run.error_message) == ("failed", None, NOT_LOGGED_IN)
    assert d.run.finished_at is not None and searcher.searched == [] and llm.calls == 0
    assert [(k.status, k.error_message) for k in d.keywords] == [("skipped", "Not searched — not logged in")] * 2
    with session_scope() as db:
        s = get_session_row(db)
        assert s is not None and (s.status, s.last_error) == ("not_connected", NOT_LOGGED_IN)
        assert runs.safety_cooldown_until(db) is None  # not a safety stop
        e = runs.run_eligibility(db)
        assert e.reason_code == "not_connected"
        assert set(db.scalars(select(Keyword.last_run_at))) == {None}


def test_the_keyword_that_tripped_a_stop_is_stamped_with_the_stop_time(db_url: str) -> None:
    setup("ai workshop", "captcha-test", "ai consulting")
    before = datetime.now(UTC)
    searcher = FakeSearcher({"ai workshop": [post(5, TEXT[5])], "captcha-test": safety("captcha")})
    rid = run_job(searcher)
    after = datetime.now(UTC)
    from sales0.clock import ensure_utc

    with session_scope() as db:
        stamps = {k.text: k.last_run_at for k in db.scalars(select(Keyword))}
        run = db.get(ScrapeRun, rid)
        assert run is not None and run.finished_at is not None
        assert stamps["ai consulting"] is None
        stopped = ensure_utc(stamps["captcha-test"])
        assert before <= stopped <= ensure_utc(run.finished_at) <= after
        # The next plan opens with the never-run keyword and ends with the one that tripped LinkedIn.
        assert [t for _i, t in discovery_job.select_keywords(db, 10)] == ["ai consulting", "ai workshop",
                                                                           "captcha-test"]


def test_a_field_missing_on_half_the_posts_is_a_layout_warning_and_a_small_miss_is_written_down(
        db_url: str) -> None:
    setup("ai workshop", "ai consulting")
    searcher = FakeSearcher(
        {"ai workshop": [post(5, TEXT[5]), post(6, TEXT[6]), post(1, TEXT[1]), post(8, TEXT[8])],
         "ai consulting": [post(9, TEXT[9]), post(3, TEXT[3])]},
        extra={"ai workshop": {"field_misses": {"author_name": 2}, "cards_found": 4},
               "ai consulting": {"field_misses": {"author_headline": 0}, "parse_errors": 1, "cards_found": 3}},
    )
    with capture_logs() as logs:
        d = detail(run_job(searcher))
    assert d.run.status == "succeeded" and d.run.layout_warnings == 1
    by_kw = {k.keyword_text: k for k in d.keywords}
    assert (by_kw["ai workshop"].status, by_kw["ai workshop"].error_message) == (
        "warning", "Parsed 4 posts, but couldn't read author on 2 — LinkedIn layout may have changed")
    assert (by_kw["ai consulting"].status, by_kw["ai consulting"].error_message) == (
        "done", "1 card couldn't be read")
    assert by_kw["ai consulting"].errors == 1  # parse errors still count as errors
    kw_logs = {e["keyword"]: e for e in logs if e["event"] == "scrape_keyword"}
    warn = kw_logs["ai workshop"]
    assert (warn["log_level"], warn["status"], warn["drift"], warn["cards_found"]) == ("warning", "warning", False, 4)
    assert warn["field_misses"] == {"author_name": 2} and warn["no_results"] is False
    ok = kw_logs["ai consulting"]
    assert (ok["log_level"], ok["parse_errors"]) == ("info", 1)
    run_log = next(e for e in logs if e["event"] == "scrape_run")
    assert run_log["layout_warnings"] == 1 and run_log["browser"] == "chromium"


def test_drift_logs_a_warning_with_drift_true(db_url: str) -> None:
    setup("layout-drift-test")
    searcher = FakeSearcher({"layout-drift-test": []}, drift={"layout-drift-test"},
                            extra={"layout-drift-test": {"cards_found": 3}})
    with capture_logs() as logs:
        d = detail(run_job(searcher))
    assert (d.run.status, d.run.layout_warnings, d.keywords[0].status) == ("succeeded", 1, "warning")
    line = next(e for e in logs if e["event"] == "scrape_keyword")
    assert (line["log_level"], line["drift"], line["cards_found"]) == ("warning", True, 3)


def test_run_start_logs_the_linkedin_session_line_without_cookie_values(db_url: str) -> None:
    setup("ai workshop")
    with capture_logs() as logs:
        run_job(FakeSearcher({}, enter_error=session_missing()))
    line = next(e for e in logs if e["event"] == "linkedin_session")
    assert (line["action"], line["result"], line["reason"], line["cookie_present"], line["headless"]) == (
        "run_start", "not_connected", "no_cookie", False, True)
    assert line["final_path"] == "/__fixtures/linkedin/feed/" and line["browser"] == "chromium"


# ---------------------------------------------------------------------- Phase 1.2
SDUI_TEXT = {
    1: TEXT[1] + " — 40 managers, offline, week of 17 Nov; proposals to training@acmelogistics.example please.",
    2: TEXT[2] + " — send trainer profile and per-day commercials to trainers@skillbridge.example today.",
}


def test_scrape_keyword_logs_strategy_and_permalink_counters(db_url: str) -> None:
    setup("ai workshop", "legacy-urn-test", "layout-drift-test")
    searcher = FakeSearcher(
        {"ai workshop": [sdui_post("K1", SDUI_TEXT[1]), sdui_post("K2", SDUI_TEXT[2], slug="in/rahul-fx",
                                                                    urn="urn:li:activity:7300000000000000002")],
         "legacy-urn-test": [post(5, TEXT[5])]},
        drift={"layout-drift-test"},
        extra={"ai workshop": {"strategy": "sdui", "cards_found": 2, "passes": 2, "expanded": 1, "stop_reason": "no_growth"},
               "legacy-urn-test": {"strategy": "urn", "cards_found": 1},
               "layout-drift-test": {"strategy": "none", "cards_found": 3}},
    )
    with capture_logs() as logs:
        d = detail(run_job(searcher))
    assert d.run.status == "succeeded" and (d.run.posts_seen, d.run.posts_new) == (3, 3)
    kw = {e["keyword"]: e for e in logs if e["event"] == "scrape_keyword"}
    # Only the profile-linked SDUI card (K1) needs a menu click; K2 / the URN card already carry a post link.
    assert {k: (v["strategy"], v["permalink_attempts"], v["permalinks_found"]) for k, v in kw.items()} == {
        "ai workshop": ("sdui", 1, 1), "legacy-urn-test": ("urn", 0, 0), "layout-drift-test": ("none", 0, 0)}
    assert (kw["ai workshop"]["passes"], kw["ai workshop"]["expanded"], kw["ai workshop"]["stop_reason"]) == (
        2, 1, "no_growth")
    assert kw["ai workshop"]["posts_relevant"] == 2 and kw["ai workshop"]["scrolls"] == 3
    assert kw["layout-drift-test"]["log_level"] == "warning"
    run_log = next(e for e in logs if e["event"] == "scrape_run")
    assert (run_log["posts_relevant"], run_log["permalinks_found"], run_log["relevance_pct"]) == (3, 1, 100.0)
    assert next(e for e in logs if e["event"] == "seen_posts_purged")["rows"] == 0


def test_result_without_phase_1_2_fields_logs_safe_defaults(db_url: str) -> None:
    # A searcher whose result object lacks the new attributes must not break the log line.
    setup("ai consulting")

    class Bare:
        keyword = "ai consulting"
        posts = [post(8, TEXT[8])]
        scrolls = 1
        parse_errors = 0
        drift_warning = False

    class Old(FakeSearcher):
        def scan(self, keyword: str, max_posts: int):  # type: ignore[override]
            self.searched.append(keyword)
            return Bare()

    with capture_logs() as logs:
        d = detail(run_job(Old({})))
    assert d.run.posts_new == 1
    line = next(e for e in logs if e["event"] == "scrape_keyword")
    assert (line["strategy"], line["permalink_attempts"], line["permalinks_found"]) == ("none", 0, 0)


def test_sdui_run_dedupes_changed_keys_and_upgrades_links_across_runs(db_url: str) -> None:
    """Two runs on scripted SDUI posts: P1's key differs per keyword (content merge), P2 is first
    linked to the author profile, then a later run learns its URN (source row + permalink)."""
    setup("corporate ai training", "ai workshop")
    run1 = FakeSearcher({
        "corporate ai training": [sdui_post("P1-cat", SDUI_TEXT[1]), sdui_post("P2", SDUI_TEXT[2], slug="in/rahul-fx")],
        "ai workshop": [sdui_post("P1-aw", SDUI_TEXT[1])],
    })
    run1.link_map = {}  # "Copy link to post" misses: the cards stay linked to the author profile
    d1 = detail(run_job(run1))
    assert (d1.run.posts_seen, d1.run.posts_new, d1.run.posts_dupe, d1.run.enquiries_triaged) == (3, 2, 1, 2)
    with session_scope() as db:
        p1 = db.scalar(select(EnquirySource.enquiry_id).where(EnquirySource.source_ref == "ck:P1-cat"))
        assert db.scalar(select(EnquirySource.enquiry_id).where(EnquirySource.source_ref == "ck:P1-aw")) == p1
        p2 = db.scalar(select(EnquirySource.enquiry_id).where(EnquirySource.source_ref == "ck:P2"))
        assert db.get(Enquiry, p2).source_url == "https://www.linkedin.com/in/rahul-fx/"  # type: ignore[union-attr]

    urn = "urn:li:activity:7300000000000000002"
    run2 = FakeSearcher({
        "corporate ai training": [sdui_post("P2-new", SDUI_TEXT[2], slug="in/rahul-fx", urn=urn)],
        "ai workshop": [sdui_post("P1-aw", SDUI_TEXT[1])],
    })
    run2.link_map = {}
    d2 = detail(run_job(run2))
    assert (d2.run.posts_seen, d2.run.posts_new, d2.run.posts_dupe, d2.run.enquiries_triaged) == (2, 0, 2, 0)
    with session_scope() as db:
        assert db.scalar(select(func.count(Enquiry.id))) == 2
        assert db.scalar(select(EnquirySource.enquiry_id).where(EnquirySource.source_ref == urn)) == p2
        assert db.get(Enquiry, p2).source_url == f"https://www.linkedin.com/feed/update/{urn}/"  # type: ignore[union-attr]


# ---------------------------------------------------------------------- Phase 1.4
BUYER = "Northwind Bank needs a corporate GenAI trainer for 60 managers in Mumbai next month"
AGENCY = "We are an agency hiring freelance AI trainers for our client roster, send CVs"
EVENT = "Join our free AI summit this Saturday, register now and bring your team"


def oracle_llm() -> FakeLLMClient:
    """Genuine only for the buyer-style texts (``Northwind`` / ``Acme``); everything else is a discard."""
    return FakeLLMClient.with_oracle(lambda t: "genuine" if ("Northwind" in t or "Acme" in t) else "discard")


def sdui_corpus() -> dict[str, list]:
    return {
        "looking for ai trainer": [
            sdui_post("A1", BUYER, slug="in/buyer-one", author="Buyer One"),
            sdui_post("A2", AGENCY, slug="in/agency-two", author="Agency Two"),
            sdui_post("A3", EVENT, slug="in/event-three", author="Event Three"),
        ],
        "corporate ai training": [
            sdui_post("B1", TEXT[1], slug="in/buyer-four", author="Buyer Four"),
            sdui_post("A2b", AGENCY, slug="in/agency-two", author="Agency Two"),
        ],
    }


def test_only_genuine_posts_become_enquiries_and_the_summary_is_honest(db_url: str) -> None:
    setup("looking for ai trainer", "corporate ai training")
    llm = oracle_llm()
    d = detail(run_job(FakeSearcher(sdui_corpus()), llm))
    r = d.run
    assert r.status == "succeeded"
    assert (r.posts_seen, r.posts_new, r.posts_dupe) == (5, 4, 1)
    assert (r.posts_relevant, r.posts_skipped_ai, r.triage_failed) == (2, 2, 0)
    assert r.posts_new == r.posts_relevant + r.posts_skipped_ai + r.triage_failed
    assert llm.calls == 4
    assert r.current_step == "Run finished — scanned 5 · new 4 · relevant 2 (50%) · skipped by AI 2 · already seen 1"
    assert r.relevance_pct == 50.0
    with session_scope() as db:
        assert db.scalar(select(func.count(Enquiry.id))) == 2
        discards = db.scalars(select(SeenPost).where(SeenPost.verdict == "discard")).all()
        assert len(discards) == 2 and all(x.enquiry_id is None and x.raw_text for x in discards)
        genuine = db.scalars(select(SeenPost).where(SeenPost.verdict == "genuine")).all()
        assert len(genuine) == 2 and all(x.enquiry_id is not None and x.raw_text is None for x in genuine)
        # authors are remembered once each; the repeat agency post bumped posts_seen
        agency = db.scalars(select(LinkedinProfile).where(LinkedinProfile.slug == "in/agency-two")).one()
        assert agency.posts_seen == 2 and agency.relevant_count == 0
        assert db.scalar(select(func.count(LinkedinProfile.id))) == 4
        by_kw = {k.text: k for k in db.scalars(select(Keyword))}
        assert (by_kw["looking for ai trainer"].scanned_total, by_kw["looking for ai trainer"].relevant_total) == (3, 1)
        assert (by_kw["corporate ai training"].last_scanned, by_kw["corporate ai training"].last_relevant) == (2, 1)
    assert [(k.posts_seen, k.posts_relevant, k.posts_skipped_ai) for k in d.keywords] == [(3, 1, 2), (2, 1, 0)]
    assert all(k.cost_usd_micros > 0 for k in d.keywords if k.posts_new)


def test_per_keyword_order_is_scan_then_triage_then_capture_links_for_genuine_posts_only(db_url: str) -> None:
    setup("looking for ai trainer", "corporate ai training")
    searcher = FakeSearcher(sdui_corpus())
    llm = oracle_llm()
    calls_before_capture: list[int] = []
    real = searcher.capture_links
    searcher.capture_links = lambda refs: (calls_before_capture.append(llm.calls), real(refs))[1]  # type: ignore[method-assign]
    run_job(searcher, llm)
    assert [e[:2] for e in searcher.events] == [("scan", "looking for ai trainer"), ("capture", "looking for ai trainer"),
                                                 ("scan", "corporate ai training"), ("capture", "corporate ai training")]
    assert searcher.captured == [["ck:A1"], ["ck:B1"]]  # discards (A2, A3) are never clicked
    assert calls_before_capture == [3, 4]  # all of the keyword's triage calls finished before its clicks
    with session_scope() as db:
        a1 = db.scalars(select(SeenPost).where(SeenPost.source_ref == "ck:A1")).one()
        assert a1.post_url == permalink_for("ck:A1")
        assert db.get(Enquiry, a1.enquiry_id).source_url == permalink_for("ck:A1")  # type: ignore[union-attr]
        a2 = db.scalars(select(SeenPost).where(SeenPost.source_ref == "ck:A2")).one()
        assert a2.post_url is None


def test_a_second_identical_run_is_idempotent(db_url: str) -> None:
    setup("looking for ai trainer", "corporate ai training")
    run_job(FakeSearcher(sdui_corpus()), oracle_llm())
    with session_scope() as db:
        counts1 = (db.scalar(select(func.count(Enquiry.id))), db.scalar(select(func.count(SeenPost.id))),
                   db.scalar(select(func.count(LinkedinProfile.id))))
        seen1 = db.scalars(select(LinkedinProfile).where(LinkedinProfile.slug == "in/buyer-one")).one().posts_seen
    llm2 = oracle_llm()
    searcher2 = FakeSearcher(sdui_corpus())
    d2 = detail(run_job(searcher2, llm2))
    r = d2.run
    assert (r.posts_seen, r.posts_new, r.posts_relevant, r.posts_skipped_ai, r.posts_dupe) == (5, 0, 0, 0, 5)
    assert llm2.calls == 0 and searcher2.captured == []  # genuine posts already have their permalink
    assert (r.current_step or "").startswith("Run finished — scanned 5 · new 0 · relevant 0 · skipped by AI 0 · already seen 5")
    with session_scope() as db:
        assert (db.scalar(select(func.count(Enquiry.id))), db.scalar(select(func.count(SeenPost.id))),
                db.scalar(select(func.count(LinkedinProfile.id)))) == counts1
        assert db.scalars(select(LinkedinProfile).where(LinkedinProfile.slug == "in/buyer-one")).one().posts_seen == seen1 + 1


def test_a_seen_genuine_post_without_a_permalink_is_retried_on_the_next_run(db_url: str) -> None:
    setup("looking for ai trainer")
    data = {"looking for ai trainer": [sdui_post("A1", BUYER, slug="in/buyer-one")]}
    first = FakeSearcher(data, link_map={})  # the click found nothing
    run_job(first, oracle_llm())
    assert first.captured == [["ck:A1"]]
    with session_scope() as db:
        assert db.scalars(select(SeenPost)).one().post_url is None
    second = FakeSearcher(data)
    d = detail(run_job(second, oracle_llm()))
    assert second.captured == [["ck:A1"]] and d.run.permalinks_found == 1
    with session_scope() as db:
        assert db.scalars(select(SeenPost)).one().post_url == permalink_for("ck:A1")


def test_triage_runs_concurrently_but_never_more_than_four_at_once(db_url: str) -> None:
    import threading
    import time

    setup("looking for ai trainer")
    posts = [sdui_post(f"C{i}", f"{BUYER} variant {i} with extra context words {i * 7919}", slug=f"in/c-{i}") for i in range(12)]
    lock = threading.Lock()
    state = {"now": 0, "max": 0}

    def respond(_req):  # noqa: ANN001
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return triage_answer()

    d = detail(run_job(FakeSearcher({"looking for ai trainer": posts}), FakeLLMClient(responder=respond)))
    assert d.run.posts_relevant == 12 and d.run.status == "succeeded"
    assert 2 <= state["max"] <= 4


def test_purge_runs_at_run_start_and_a_purged_post_found_again_makes_no_row_and_no_call(db_url: str) -> None:
    setup("looking for ai trainer")
    data = {"looking for ai trainer": [sdui_post("A2", AGENCY, slug="in/agency-two")]}
    run_job(FakeSearcher(data), oracle_llm())
    with session_scope() as db:
        row = db.scalars(select(SeenPost)).one()
        assert row.raw_text and row.raw_purge_after is not None
        row.raw_purge_after = datetime.now(UTC) - timedelta(days=1)
    llm = oracle_llm()
    with capture_logs() as logs:
        d = detail(run_job(FakeSearcher(data), llm))
    assert next(e for e in logs if e["event"] == "seen_posts_purged")["rows"] == 1
    assert llm.calls == 0 and (d.run.posts_new, d.run.posts_dupe) == (0, 1)
    with session_scope() as db:
        row = db.scalars(select(SeenPost)).one()
        assert row.raw_text is None and row.raw_purged_at is not None and row.snippet
        assert db.scalar(select(func.count(SeenPost.id))) == 1


def test_a_safety_stop_while_copying_links_keeps_everything_already_saved(db_url: str) -> None:
    setup("looking for ai trainer", "corporate ai training")
    searcher = FakeSearcher(sdui_corpus(), capture_error=safety("captcha"))
    d = detail(run_job(searcher, oracle_llm()))
    assert (d.run.status, d.run.abort_reason) == ("aborted", "captcha")
    assert searcher.searched == ["looking for ai trainer"]
    assert [k.status for k in d.keywords] == ["error", "skipped"]
    assert (d.run.posts_seen, d.run.posts_relevant, d.run.posts_skipped_ai) == (3, 1, 2)
    assert (d.run.current_step or "").startswith("Run stopped: LinkedIn captcha detected — scanned 3 · new 3 · relevant 1")
    with session_scope() as db:
        assert db.scalar(select(func.count(Enquiry.id))) == 1 and db.scalar(select(func.count(SeenPost.id))) == 3
        stamps = {k.text: k.last_run_at for k in db.scalars(select(Keyword))}
        assert stamps["looking for ai trainer"] is not None and stamps["corporate ai training"] is None
        kw = db.scalars(select(Keyword).where(Keyword.text_key == "looking for ai trainer")).one()
        assert kw.scanned_total == 3


def test_an_aborted_run_is_continued_with_the_unreached_keywords_first(db_url: str) -> None:
    setup("ai workshop", "captcha-test", "ai consulting")
    first = FakeSearcher({"ai workshop": [post(5, TEXT[5])], "captcha-test": safety("captcha")})
    assert detail(run_job(first)).run.abort_reason == "captcha"
    with session_scope() as db:
        set_session_status("connected")
        order = [t for _i, t in discovery_job.select_keywords(db, 10)]
    assert order[0] == "ai consulting"


def test_a_failed_triage_is_remembered_as_failed_not_as_a_relevant_post(db_url: str) -> None:
    setup("looking for ai trainer")
    llm = FakeLLMClient(responder=lambda req: None)  # no tool call, twice (call + repair)
    d = detail(run_job(FakeSearcher({"looking for ai trainer": [sdui_post("A1", BUYER, slug="in/buyer-one")]}), llm))
    assert d.run.status == "succeeded" and (d.run.posts_new, d.run.triage_failed, d.run.posts_relevant) == (1, 1, 0)
    with session_scope() as db:
        assert db.scalar(select(func.count(Enquiry.id))) == 0
        row = db.scalars(select(SeenPost)).one()
        assert row.triage_status == "failed" and row.triage_error and row.verdict is None


def test_the_per_keyword_cap_passed_to_scan_shrinks_with_the_daily_budget(db_url: str,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_MAX_POSTS_PER_DAY", "100")
    monkeypatch.setenv("SALES0_SCRAPE_MAX_POSTS_PER_KEYWORD", "80")
    reset_settings()
    setup("ai workshop", "ai consulting")
    searcher = FakeSearcher(corpus())
    run_job(searcher)
    assert searcher.max_posts_seen[0] == 80
    assert searcher.max_posts_seen[1] == min(80, 100 - 4)  # 4 posts were scanned by the first keyword


def test_the_run_start_is_the_login_check_before_any_scan(db_url: str) -> None:
    setup("ai workshop")
    searcher = FakeSearcher({}, enter_error=session_missing())
    d = detail(run_job(searcher))
    assert d.run.status == "failed" and searcher.events == []
    with session_scope() as db:
        assert db.scalar(select(func.count(SeenPost.id))) == 0
