"""Discovery runs end-to-end: services.runs.start_run -> runner thread -> real LinkedInSearcher
(headless Chromium) on the fixture LinkedIn corpus -> dedupe -> triage.

``llm``-marked tests use the REAL Haiku key from .env (gate assertions 2-5). The FakeLLM
variants exercise the same browser + DB path when only the scraper/dedupe facts matter.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from sales0.clock import utcnow
from sales0.config import Settings, get_settings, reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, EnquiryKeywordHit, EnquirySource, LlmCall, ScrapeRun
from sales0.jobs import discovery_job
from sales0.jobs.runner import get_runner
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


def enquiry_for(n: int) -> Enquiry:
    with session_scope() as db:
        eid = db.scalar(select(EnquirySource.enquiry_id).where(EnquirySource.source_ref == URN.format(n)))
        e = db.get(Enquiry, eid)
        assert e is not None
        return e


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
    p1, p2 = enquiry_for(1), enquiry_for(2)
    assert chips(p1.id) == {"corporate ai training", "ai workshop"}
    assert chips(p2.id) == {"corporate ai training", "ai workshop"}
    with session_scope() as db:
        assert db.scalar(select(func.count(Enquiry.id))) == 7
        assert db.scalar(select(func.count(EnquirySource.id)).where(EnquirySource.enquiry_id == p2.id)) == 2
    assert p1.source_url == f"{discovery_base()}/feed/update/{URN.format(1)}/"


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


def test_cleared_profile_aborts_with_logout(db_url: str, fixture_settings: Settings) -> None:
    set_session_status("connected")  # DB says connected, but the profile has no cookie
    add_keywords("ai workshop")
    d = detail(start_and_wait())
    assert (d.run.status, d.run.abort_reason) == ("aborted", "logout")
    assert [k.status for k in d.keywords] == ["skipped"]


@pytest.mark.llm
def test_invalid_key_run_fails_with_clear_message_and_enquiries_stay_pending(
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
        assert set(db.scalars(select(Enquiry.triage_status))) == {"pending"}
        assert db.scalar(select(func.count(Enquiry.id))) == 4


@pytest.mark.llm
def test_real_haiku_run_one_then_run_two_learns_from_decisions(connected: Settings) -> None:
    add_keywords(*RUN1_KEYWORDS)
    run1 = detail(start_and_wait())
    assert run1.run.status == "succeeded", run1.run.error_message
    r = run1.run
    assert (r.posts_seen, r.posts_new, r.posts_dupe, r.enquiries_triaged) == (9, 7, 2, 7)
    assert r.cost_usd_micros > 0

    p1, p6, p8 = enquiry_for(1), enquiry_for(6), enquiry_for(8)
    assert (p1.ai_verdict, p1.email) == ("genuine", "training@acmelogistics.example")
    for n in (2, 3, 9):
        assert enquiry_for(n).ai_verdict == "discard"
    assert p6.email is None
    with session_scope() as db:
        names = {o.id: o.name for o in offerings.list_offerings(db, include_inactive=True)}
    assert names.get(p8.ai_offering_id) == "AI consulting & build services"

    with session_scope() as db:
        inbox.decide(db, [p1.id], "accept")
        inbox.decide(db, [enquiry_for(2).id, enquiry_for(3).id], "discard", "Looking for individual trainers")
        keywords.add_keyword(db, "claude code training")
    with session_scope() as db:
        s = runs.run_eligibility(db)
        assert s.can_run, s.reason
    run2 = detail(start_and_wait())
    r2 = run2.run
    assert r2.status == "succeeded", r2.error_message
    assert run2.keywords[0].keyword_text == "claude code training"
    assert (r2.posts_seen, r2.posts_new, r2.posts_dupe, r2.enquiries_triaged) == (11, 2, 9, 2)
    with session_scope() as db:
        rows = db.scalars(select(LlmCall).where(LlmCall.run_id == r2.id, LlmCall.feature == "triage")).all()
        assert rows and all(row.fewshot_count >= 1 for row in rows)
