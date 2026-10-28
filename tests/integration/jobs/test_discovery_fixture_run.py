"""Discovery runs end-to-end: services.runs.start_run -> runner thread -> real LinkedInSearcher
(headless Chromium) on the fixture LinkedIn corpus -> dedupe -> triage.

``llm``-marked tests use the REAL Haiku key from .env (gate assertions 2-5). The FakeLLM
variants exercise the same browser + DB path when only the scraper/dedupe facts matter.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from structlog.testing import capture_logs

from sales0.clock import utcnow
from sales0.config import Settings, get_settings, reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, EnquiryKeywordHit, EnquirySource, Keyword, LlmCall, ScrapeRun, SeenPost
from sales0.jobs import discovery_job
from sales0.jobs.runner import get_runner
from sales0.linkedin_fixture.corpus import FixturePost, load_corpus
from sales0.llm import reset_llm_client
from sales0.services import inbox, keywords, offerings, runs
from sales0.services.session import session_status
from tests.integration.services.helpers import set_session_status
from tests.support.fake_llm import FakeLLMClient, triage_answer

pytestmark = pytest.mark.browser

RUN1_KEYWORDS = ("corporate ai training", "ai workshop", "ai consulting")
URN = "urn:li:activity:73000000000000000{:02d}"


def add_keywords(*texts: str) -> None:
    with session_scope() as db:
        for t in texts:
            keywords.add_keyword(db, t)


def start_and_wait(timeout: float = 300) -> int:
    with session_scope() as db:
        run = runs.start_run(db)
    assert run.status == "queued"
    assert get_runner().wait(timeout), "discovery run did not finish in time"
    return run.id


def detail(run_id: int):
    with session_scope() as db:
        return runs.get_run(db, run_id)


def fixture_post(n: int) -> FixturePost:
    return next(p for p in load_corpus() if p.id == f"P{n}")


def enquiry_for(n: int) -> Enquiry:
    """Phase 1.2: the SDUI DOM carries no URN, so a post is located by any of its possible
    refs (its URN when "Copy link to post" yields it, else ``ck:`` + its key per keyword)."""
    refs = fixture_post(n).possible_refs()
    with session_scope() as db:
        eid = db.scalar(select(EnquirySource.enquiry_id).where(EnquirySource.source_ref.in_(refs)).limit(1))
        assert eid is not None, f"no enquiry for P{n} (refs {refs})"
        e = db.get(Enquiry, eid)
        assert e is not None
        return e


def source_refs(eid: int) -> list[str]:
    with session_scope() as db:
        return list(db.scalars(select(EnquirySource.source_ref).where(EnquirySource.enquiry_id == eid)
                               .order_by(EnquirySource.id)))


def chips(eid: int) -> set[str]:
    with session_scope() as db:
        return set(db.scalars(select(EnquiryKeywordHit.keyword_text).where(EnquiryKeywordHit.enquiry_id == eid)))


@pytest.fixture
def connected(db_url: str, connected_settings: Settings) -> Settings:
    set_session_status("connected")
    return connected_settings


def discovery_base() -> str:
    from sales0.scraper.urls import base_url

    return base_url(get_settings())


def test_fixture_run_dedupes_the_corpus_with_a_real_browser_and_fake_llm(connected: Settings) -> None:
    add_keywords(*RUN1_KEYWORDS)
    with session_scope() as db:
        run = ScrapeRun(trigger="manual", mode="fixture", status="queued", started_at=utcnow())
        db.add(run)
        db.flush()
        rid = run.id
    llm = FakeLLMClient(responder=lambda req: triage_answer())
    discovery_job.execute(rid, llm=llm)  # real LinkedInSearcher on the fixture server
    d = detail(rid)
    assert (d.run.status, d.run.posts_seen, d.run.posts_new, d.run.posts_dupe, d.run.enquiries_triaged) == (
        "succeeded", 9, 7, 2, 7)
    assert llm.calls == 7
    assert [(k.keyword_text, k.posts_seen) for k in d.keywords] == [
        ("corporate ai training", 3), ("ai workshop", 4), ("ai consulting", 2)]
    p1, p2, p5, p6 = enquiry_for(1), enquiry_for(2), enquiry_for(5), enquiry_for(6)
    assert chips(p1.id) == {"corporate ai training", "ai workshop"}
    assert chips(p2.id) == {"corporate ai training", "ai workshop"}
    with session_scope() as db:
        assert db.scalar(select(func.count(Enquiry.id))) == 7
    base = discovery_base()
    # Phase 1.2: no URN in the DOM. P1's key differs per keyword -> 2 ck: rows, merged by content.
    fp1 = fixture_post(1)
    assert source_refs(p1.id) == [f"ck:{fp1.key_for('corporate ai training')}", f"ck:{fp1.key_for('ai workshop')}"]
    # P2's URN comes from "Copy link to post" (added next to its ck: row); P7 (same text) adds its own ck: row.
    assert set(source_refs(p2.id)) == {f"ck:{fixture_post(2).key_for('corporate ai training')}", URN.format(2),
                                       f"ck:{fixture_post(7).key_for('ai workshop')}"}
    # Links: P5 copy link -> clean permalink; P1/P6 menus copy nothing -> author profile.
    assert p5.source_url == f"{base}/feed/update/{URN.format(5)}/" and URN.format(5) in source_refs(p5.id)
    # Phase 1.4: a profile fallback link is the canonical live-shaped profile URL, even in fixture mode.
    assert p1.source_url == f"https://www.linkedin.com/in/{fp1.profile_slug}/"
    assert p6.source_url == f"https://www.linkedin.com/in/{fixture_post(6).profile_slug}/"
    with session_scope() as db:
        cards = {n: inbox.get_card(db, e.id) for n, e in ((1, p1), (5, p5), (6, p6))}
    assert {n: c.link_kind for n, c in cards.items()} == {1: "profile", 5: "post", 6: "profile"}
    assert all("utm_" not in (c.source_url or "") and "rcm=" not in (c.source_url or "") for c in cards.values())


def test_checkpoint_keyword_aborts_the_run_and_flags_the_session(connected: Settings) -> None:
    add_keywords("checkpoint-test", "ai workshop")
    rid = start_and_wait()
    d = detail(rid)
    assert (d.run.status, d.run.abort_reason) == ("aborted", "checkpoint")
    assert [k.status for k in d.keywords] == ["error", "skipped"]
    with session_scope() as db:
        status = session_status(db)
        assert status.status == "needs_attention" and "Checkpoint page" in (status.last_error or "")
        assert runs.run_eligibility(db).reason_code == "needs_attention"
        assert db.scalar(select(func.count(Enquiry.id))) == 0


@pytest.mark.parametrize(("keyword", "reason"), [("captcha-test", "captcha"), ("ratelimit-test", "rate_limited")])
def test_captcha_and_rate_limit_keywords_abort(connected: Settings, keyword: str, reason: str) -> None:
    add_keywords(keyword)
    d = detail(start_and_wait())
    assert (d.run.status, d.run.abort_reason) == ("aborted", reason)


def test_profile_without_a_session_cookie_fails_the_run_as_not_connected(db_url: str,
                                                                        fixture_settings: Settings) -> None:
    set_session_status("connected")  # DB says connected (e.g. Phase 1), but this profile has no li_at
    add_keywords("ai workshop", "ai consulting")
    d = detail(start_and_wait())
    assert (d.run.status, d.run.abort_reason) == ("failed", None)
    assert d.run.error_message == "Not logged in to LinkedIn in sales0's browser — press Connect LinkedIn in Setup"
    assert [k.status for k in d.keywords] == ["skipped", "skipped"]
    with session_scope() as db:
        s = session_status(db)
        assert s.stored_status == "not_connected" and s.cooldown_until is None
        assert runs.run_eligibility(db).reason_code == "not_connected"


def test_logout_keyword_aborts_with_logout_and_stamps_that_keyword(connected: Settings) -> None:
    add_keywords("logout-test", "ai workshop")
    d = detail(start_and_wait())
    assert (d.run.status, d.run.abort_reason) == ("aborted", "logout")
    assert [k.status for k in d.keywords] == ["error", "skipped"]
    with session_scope() as db:
        assert session_status(db).stored_status == "needs_attention"
        stamps = {k.text: k.last_run_at for k in db.scalars(select(Keyword))}
    assert stamps["logout-test"] is not None and stamps["ai workshop"] is None


def test_checkpoint_during_scroll_aborts_at_the_scroll_checkpoint(connected: Settings) -> None:
    add_keywords("checkpoint-scroll-test")
    d = detail(start_and_wait())
    assert (d.run.status, d.run.abort_reason) == ("aborted", "checkpoint")


def test_layout_drift_keyword_is_a_warning_and_counts_as_a_layout_warning(connected: Settings) -> None:
    add_keywords("layout-drift-test", "ai consulting")
    d = detail(start_and_wait())
    assert d.run.status == "succeeded" and d.run.layout_warnings == 1
    by_kw = {k.keyword_text: k for k in d.keywords}
    assert by_kw["layout-drift-test"].status == "warning"
    assert (by_kw["layout-drift-test"].error_message or "").startswith(
        "No posts parsed — LinkedIn layout may have changed")
    assert by_kw["ai consulting"].status == "done" and by_kw["ai consulting"].posts_seen == 2


@pytest.mark.llm
def test_invalid_key_run_fails_with_clear_message_and_posts_stay_pending(
    connected: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALES0_ANTHROPIC_API_KEY", "invalid")
    reset_settings()
    reset_llm_client()
    add_keywords("ai workshop")
    d = detail(start_and_wait())
    assert d.run.status == "failed"
    assert "Anthropic rejected the key" in (d.run.error_message or "")
    with session_scope() as db:
        # Phase 1.4: no enquiry exists before a verdict; the 4 scanned posts wait as pending seen_posts.
        assert db.scalar(select(func.count(Enquiry.id))) == 0
        assert set(db.scalars(select(SeenPost.triage_status))) == {"pending"}
        assert db.scalar(select(func.count(SeenPost.id))) == 4


@pytest.mark.llm
def test_real_haiku_run_one_then_run_two_learns_from_decisions(connected: Settings) -> None:
    add_keywords(*RUN1_KEYWORDS)
    run1 = detail(start_and_wait())
    assert run1.run.status == "succeeded", run1.run.error_message
    r = run1.run
    assert (r.posts_seen, r.posts_new, r.posts_dupe, r.enquiries_triaged) == (9, 7, 2, 7)
    assert (r.posts_relevant, r.posts_skipped_ai) == (4, 3)  # gate 6: the AI keeps only real buyers
    assert r.cost_usd_micros > 0

    p1, p6, p8 = enquiry_for(1), enquiry_for(6), enquiry_for(8)
    assert (p1.ai_verdict, p1.email) == ("genuine", "training@acmelogistics.example")
    assert p6.email is None
    with session_scope() as db:
        assert db.scalar(select(func.count(Enquiry.id))) == 4
        skipped = {x.snippet for x in db.scalars(select(SeenPost).where(SeenPost.verdict == "discard"))}
        assert len(skipped) == 3 and all(x.enquiry_id is None for x in db.scalars(select(SeenPost).where(SeenPost.verdict == "discard")))
        names = {o.id: o.name for o in offerings.list_offerings(db, include_inactive=True)}
    assert names.get(p8.ai_offering_id) == "AI consulting & build services"

    with session_scope() as db:
        inbox.decide(db, [p1.id], "accept")
        inbox.decide(db, [p6.id], "discard", "Not a real requirement")
        keywords.add_keyword(db, "claude code training")
    with session_scope() as db:
        s = runs.run_eligibility(db)
        assert s.can_run, s.reason
    run2 = detail(start_and_wait())
    r2 = run2.run
    assert r2.status == "succeeded", r2.error_message
    assert run2.keywords[0].keyword_text == "claude code training"
    assert (r2.posts_seen, r2.posts_new, r2.posts_dupe, r2.enquiries_triaged) == (11, 2, 9, 2)
    assert (r2.posts_relevant, r2.posts_skipped_ai) == (1, 1)
    with session_scope() as db:
        rows = db.scalars(select(LlmCall).where(LlmCall.run_id == r2.id, LlmCall.feature == "triage")).all()
        assert rows and all(row.fewshot_count >= 1 for row in rows)
        assert len(rows) == 2  # the 9 already-seen posts cost nothing


def test_fixture_run_logs_sdui_strategy_and_permalink_counts(connected: Settings) -> None:
    add_keywords("ai workshop")
    with session_scope() as db:
        run = ScrapeRun(trigger="manual", mode="fixture", status="queued", started_at=utcnow())
        db.add(run)
        db.flush()
        rid = run.id
    with capture_logs() as logs:
        discovery_job.execute(rid, llm=FakeLLMClient(responder=lambda req: triage_answer()))
    d = detail(rid)
    assert (d.run.status, d.run.posts_seen, d.run.posts_new) == ("succeeded", 4, 4)
    line = next(e for e in logs if e["event"] == "scrape_keyword")
    # ai workshop = P1 (noop), P5 (urn), P6 (noop), P7 (no menu): 3 attempts, 1 link found.
    assert (line["strategy"], line["permalink_attempts"], line["permalinks_found"]) == ("sdui", 3, 1)


def test_legacy_urn_keyword_still_identifies_posts_by_urn(connected: Settings) -> None:
    add_keywords("legacy-urn-test")
    d = detail(start_and_wait())
    assert d.run.status == "succeeded" and d.keywords[0].status == "done"
    assert d.run.posts_seen == 10
    with session_scope() as db:
        refs = set(db.scalars(select(EnquirySource.source_ref)))
    assert refs and all(r.startswith("urn:li:activity:") for r in refs)
