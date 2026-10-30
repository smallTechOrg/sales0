"""Research on accept and the bulk "Research active enquiries" run: company + enquiry + people, derived 'pending'."""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, Prospect, ProspectDoc
from sales0.services import inbox
from sales0.services.crm import prospects
from sales0.services.crm.actor import Actor
from sales0.services.research import auto, docs
from sales0.services.research import prospect as research
from sales0.web.routes import pipeline as pipeline_routes
from tests.integration.research.conftest import HX, brief, make_prospect, mock_fetcher, responder
from tests.support.crm_seed import seed_pending_enquiries
from tests.support.fake_llm import FakeLLMClient

PEOPLE = [{"name": "Priya Menon", "title": "Head of Operations", "summary": "Runs warehouse operations.", "approach": "Lead with ROI."}]
INSIGHTS = ["Asks for a two-day GenAI workshop for 40 warehouse planners in Pune."]


def _v3_responder() -> Any:
    base = responder()

    def answer(req: Any) -> Any:
        out = base(req)
        if req.tool_name == "research_step" and isinstance(out, dict) and out.get("action") == "finish":
            out["brief"] = brief(enquiry_insights=INSIGHTS, people=PEOPLE)
        if req.tool_name == "record_research":
            out = {**out, "enquiry_insights": INSIGHTS, "people": PEOPLE}
        return out

    return answer


@pytest.fixture
def auto_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_AUTO_RESEARCH", "true")
    reset_settings()


@pytest.fixture
def fake() -> FakeLLMClient:
    return FakeLLMClient(responder=_v3_responder())


@pytest.fixture
def inline(monkeypatch: pytest.MonkeyPatch, fake: FakeLLMClient) -> None:
    """Background work runs inline with the fake LLM and the mock site."""
    monkeypatch.setattr(auto, "_spawn", lambda work: work())
    monkeypatch.setattr(auto, "_llm", lambda: fake)
    monkeypatch.setattr(auto, "default_fetcher", lambda: mock_fetcher())


_SEQ = itertools.count(1)


def _prospect(name: str, company: str) -> int:
    with session_scope() as db:  # own e-mail + website per company: a shared one would reuse the same company / prospect
        return prospects.create_manual(db, name=name, company=company, email=f"{name.lower()}@{company.lower()}.example",
                                       website=f"https://www.{company.lower()}.example/").id


def _accepted_enquiry(prospect_id: int | None = None) -> int:
    """An accepted enquiry; with ``prospect_id`` it is linked to that existing prospect (own author each call)."""
    n = next(_SEQ)
    with session_scope() as db:
        seeded = seed_pending_enquiries(db, count=1, slug=f"in/author-{n}", author=f"Author {n}", email=f"a{n}@acme.example")
        eid = seeded.enquiry_ids[0]
        inbox.decide(db, [eid], "accept", actor_member_id=None)
        if prospect_id is not None:
            e = db.get(Enquiry, eid)
            e.prospect_id = prospect_id
            e.company_id = None
        return eid


def _versions(pid: int) -> list[ProspectDoc]:
    with session_scope() as db:
        return list(db.scalars(select(ProspectDoc).where(ProspectDoc.prospect_id == pid, ProspectDoc.doc_type == "research")
                               .order_by(ProspectDoc.version)))


def _prospect_of(eid: int) -> int:
    with session_scope() as db:
        pid = db.get(Enquiry, eid).prospect_id
        assert pid is not None
        return pid


def test_brief_has_enquiry_insights_and_people_and_the_run_records_what_it_covered(db_url: str, fake: FakeLLMClient) -> None:
    pid = make_prospect()
    eid = _accepted_enquiry(pid)
    with session_scope() as db:
        doc, _ = research.research_prospect(db, fake, None, pid, actor=Actor("Ann"), queue_linkedin=False, fetcher=mock_fetcher())
    assert doc.data["enquiry_insights"] == INSIGHTS and doc.data["people"][0]["name"] == "Priya Menon"
    assert doc.inputs["schema"] == 4 and doc.inputs["enquiry_ids"] == [eid] and doc.inputs["trigger"] == "manual"
    step = next(r for r in fake.requests if r.tool_name == "research_step")
    text = step.messages[0]["content"]
    text = text if isinstance(text, str) else "".join(b["text"] for b in text)
    assert "[accepted]" in text and "GenAI workshop for 40 warehouse planners" in text  # the enquiry, with its status
    assert "enquiry_insights" in step.input_schema["properties"]["brief"]["properties"]


def test_web_search_also_looks_up_the_people(db_url: str, monkeypatch: pytest.MonkeyPatch, fake: FakeLLMClient) -> None:
    monkeypatch.setenv("SALES0_RESEARCH_WEB_SEARCH", "true")
    reset_settings()
    pid = make_prospect()
    _accepted_enquiry(pid)
    with session_scope() as db:
        research.research_prospect(db, fake, None, pid, actor=Actor("Ann"), queue_linkedin=False, fetcher=mock_fetcher())
    web = next(r for r in fake.requests if r.tool_name == "record_web_findings")
    assert "People to look up" in web.messages[0]["content"] and "Priya Menon" in web.messages[0]["content"]


def test_accepting_an_enquiry_starts_research_for_its_company_in_the_background(
    db_url: str, auto_on: None, inline: None, fake: FakeLLMClient, web: TestClient
) -> None:
    with session_scope() as db:
        ids = seed_pending_enquiries(db, count=2).enquiry_ids
    resp = web.post("/inbox/decide", data={"enquiry_ids": [str(ids[0])], "decision": "accept"}, headers=HX)
    assert resp.status_code == 200 and "researching in the background" in resp.text
    pid = _prospect_of(ids[0])
    rows = _versions(pid)
    assert len(rows) == 1 and rows[0].actor == "system" and '"trigger": "accepted"' in rows[0].inputs_json
    # a discard never researches; a repeat accept of the same enquiry finds the research up to date
    web.post("/inbox/decide", data={"enquiry_ids": [str(ids[1])], "decision": "discard"}, headers=HX)
    again = web.post("/inbox/decide", data={"enquiry_ids": [str(ids[0])], "decision": "accept"}, headers=HX)
    assert "researching" not in again.text and len(_versions(pid)) == 1


def test_auto_research_switch_off_runs_nothing(db_url: str, inline: None, fake: FakeLLMClient, web: TestClient) -> None:
    with session_scope() as db:
        eid = seed_pending_enquiries(db, count=1).enquiry_ids[0]
    resp = web.post("/inbox/decide", data={"enquiry_ids": [str(eid)], "decision": "accept"}, headers=HX)
    assert "researching" not in resp.text and not fake.requests


def test_a_failure_in_the_background_never_breaks_the_accept(
    db_url: str, auto_on: None, monkeypatch: pytest.MonkeyPatch, web: TestClient
) -> None:
    monkeypatch.setattr(auto, "_spawn", lambda work: work())
    monkeypatch.setattr(auto, "_llm", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with session_scope() as db:
        eid = seed_pending_enquiries(db, count=1).enquiry_ids[0]
    resp = web.post("/inbox/decide", data={"enquiry_ids": [str(eid)], "decision": "accept"}, headers=HX)
    assert resp.status_code == 200
    with session_scope() as db:
        assert db.get(Enquiry, eid).review_status == "accepted"
        assert auto.overview(db).pending_count == 1  # still pending: the bulk button picks it up


def test_pending_reasons_scope_and_human_edits(db_url: str, fake: FakeLLMClient) -> None:
    a, b, c = (_prospect(n, co) for n, co in (("Ann", "Alpha"), ("Bob", "Beta"), ("Cy", "Gamma")))
    ea, eb, ec = _accepted_enquiry(a), _accepted_enquiry(b), _accepted_enquiry(c)
    with session_scope() as db:
        db.get(Enquiry, ec).deal_status = "lost"  # parked: out of scope
        ov = auto.overview(db)
        assert set(ov.pending) == {a, b} and set(ov.pending.values()) == {"none"} and ov.in_scope == 2
        research.research_prospect(db, fake, None, a, actor=Actor("x"), queue_linkedin=False, fetcher=mock_fetcher())
        assert auto.reason_for(db, a) is None
        # a newer accepted enquiry makes it pending again
        e2 = Enquiry(content_hash="f" * 64, raw_text="Second ask", review_status="accepted", triage_status="done", prospect_id=a,
                     company_id=db.get(Enquiry, ea).company_id, deal_status="open")
        db.add(e2)
        db.flush()
        assert auto.reason_for(db, a) == "new_enquiry"
        # a person's edit on top is left alone
        research.edit_research(db, a, actor=Actor("Ann"), what_they_do="Hand-written")
        assert auto.reason_for(db, a) is None and auto.overview(db).human_edited == 1
        # an old-format version (no schema marker) is pending
        row = db.scalar(select(ProspectDoc).where(ProspectDoc.prospect_id == b))
        assert row is None
        docs.add_version(db, b, "research", origin="ai", actor=Actor("x"), data=brief(), inputs={})
        assert auto.reason_for(db, b) == "old_format"
    assert eb


def test_bulk_researches_pending_only_respects_limit_and_reports_remaining(db_url: str, fake: FakeLLMClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auto, "default_fetcher", lambda: mock_fetcher())
    pids = [_prospect(f"P{i}", f"Co{i}") for i in range(3)]
    for pid in pids:
        _accepted_enquiry(pid)
    with session_scope() as db:
        first = auto.run_pending(db, fake, limit=2)
    assert first.researched == 2 and first.remaining == 1 and "1 more pending - click again" in first.summary()
    with session_scope() as db:
        second = auto.run_pending(db, fake)
        third = auto.run_pending(db, fake)
    assert second.researched == 1 and second.remaining == 0 and third.outcomes == []
    assert "up-to-date" in third.summary()
    with session_scope() as db:
        assert db.scalar(select(func.count(ProspectDoc.id)).where(ProspectDoc.doc_type == "research")) == 3


def test_bulk_stops_after_two_failures_in_a_row(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from sales0.errors import DomainError

    def boom(*a: Any, **k: Any) -> Any:
        raise DomainError("research_failed", "AI is down.", 502)

    monkeypatch.setattr(research, "research_prospect", boom)
    for i in range(4):
        _accepted_enquiry(_prospect(f"P{i}", f"Co{i}"))
    with session_scope() as db:
        result = auto.run_pending(db, FakeLLMClient(responder=_v3_responder()))
    assert result.failed == 2 and result.remaining == 2 and result.stopped


def test_pipeline_button_runs_bulk_and_the_card_shows_pending(db_url: str, web: TestClient, fake: FakeLLMClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auto, "default_fetcher", lambda: mock_fetcher())
    web.app.dependency_overrides[pipeline_routes.get_llm] = lambda: fake
    _accepted_enquiry(make_prospect())
    page = web.get("/pipeline?tab=enquiries").text
    assert 'id="research-all-card"' in page and "1 pending" in page and "Research active enquiries (1)" in page
    resp = web.post("/pipeline/research/pending", headers={**HX})
    assert resp.status_code == 204 and "HX-Redirect" in resp.headers
    after = web.get("/pipeline?tab=enquiries").text
    assert "0 pending" in after and "1 up to date" in after
    js = web.post("/pipeline/research/pending", headers={"Accept": "application/json"})
    assert js.status_code == 200 and js.json()["data"]["researched"] == 0


def test_company_page_shows_enquiry_insights_and_people_and_keeps_them_when_edited(db_url: str, web: TestClient, fake: FakeLLMClient) -> None:
    pid = make_prospect()
    _accepted_enquiry(pid)
    with session_scope() as db:
        research.research_prospect(db, fake, None, pid, actor=Actor("Ann"), queue_linkedin=False, fetcher=mock_fetcher())
        company_id = db.get(Prospect, pid).company_id
    page = web.get(f"/companies/{company_id}").text
    assert 'id="research-enquiry"' in page and "two-day GenAI workshop" in page
    assert 'id="research-people"' in page and "Head of Operations" in page and "Lead with ROI." in page
    resp = web.post(f"/pipeline/prospects/{pid}/research/edit", headers=HX, data={
        "what_they_do": "Edited", "enquiry_insights": "Only one point", "sources": "",
    })
    assert resp.status_code == 200
    with session_scope() as db:
        data = docs.latest(db, pid, "research").data
        assert data["enquiry_insights"] == ["Only one point"] and data["people"][0]["name"] == "Priya Menon"  # people carried over
