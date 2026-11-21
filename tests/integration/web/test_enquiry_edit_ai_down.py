"""SMA-40 edit an enquiry by hand; SMA-34/39 a pasted enquiry is saved while the AI is down and the app says what the
AI did not do; SMA-30 switched-off offerings are collapsed; SMA-31 every Enquiries column shows."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from html import unescape
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.clock import local_today
from sales0.config import get_settings, reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import Company, Contact, Enquiry, TimelineEvent
from sales0.llm import LLMBadResponse, health, set_llm_client
from sales0.llm.client import StructuredRequest
from sales0.services.crm import enquiry_edit
from sales0.web.app import create_app
from tests.integration.web.conftest import HX
from tests.support.fake_llm import FakeLLMClient, triage_answer

POST = "Hiring a GenAI trainer: Pasted Logistics needs a two-day GenAI workshop for 30 managers in Pune next month."
CREDITS = "Your credit balance is too low to access the Anthropic API."


class Broken(FakeLLMClient):
    def __init__(self) -> None:
        super().__init__(responder=lambda req: {})

    def structured(self, req: StructuredRequest) -> Any:
        raise LLMBadResponse(CREDITS)


@pytest.fixture
def web(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SALES0_ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    reset_settings()
    health.reset()
    set_llm_client(Broken())
    with TestClient(create_app()) as c:
        yield c
    set_llm_client(None)
    health.reset()
    reset_settings()


def _add(web: TestClient) -> tuple[int, int]:
    r = web.post("/enquiries/add", data={"text": POST, "accept": "1"}, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as db:
        e = db.scalar(select(Enquiry))
        assert e.review_status == "accepted" and e.triage_status == "failed" and e.company_id is not None
        assert r.headers["location"] == f"/companies/{e.company_id}#enquiry-edit-{e.id}"
        return e.id, int(e.company_id)


def test_pasted_enquiry_is_kept_when_the_ai_is_down_and_filled_in_by_hand(web: TestClient) -> None:
    eid, cid = _add(web)
    page = unescape(web.get(f"/companies/{cid}").text)
    assert f'id="enquiry-edit-{eid}" open' in page and "The AI could not read this enquiry" in page
    # SMA-39: the app says what the AI did not do
    assert 'id="ai-missed-banner"' in page and "1 enquiry not read by the AI" in page
    r = web.post(f"/enquiries/{eid}/edit", data={
        "requirement_summary": "GenAI workshop for 30 managers", "company": "Pasted Logistics", "contact_person": "Ravi Kumar",
        "email": "ravi@pasted.example", "source_url": "https://www.linkedin.com/posts/ravi-123", "date_posted": local_today(get_settings().tz).isoformat(),
        "city": "Pune", "notes": "Wants it before March", "next": f"/companies/{cid}"}, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as db:
        e = db.get(Enquiry, eid)
        assert (e.requirement_summary, e.company, e.source_url, e.city, e.notes) == (
            "GenAI workshop for 30 managers", "Pasted Logistics", "https://www.linkedin.com/posts/ravi-123", "Pune", "Wants it before March")
        assert db.get(Company, e.company_id).name == "Pasted Logistics"
        contact = db.get(Contact, e.contact_id)
        assert contact.email == "ravi@pasted.example" and contact.full_name == "Ravi Kumar"
        assert db.scalar(select(TimelineEvent.body).where(TimelineEvent.body.like("Enquiry edited:%"))) is not None
        new_cid = e.company_id
    assert r.headers["location"] == f"/companies/{new_cid}#enquiry-{eid}"


def test_edit_validation_and_the_lazy_inbox_form(web: TestClient) -> None:
    eid, cid = _add(web)
    js = {**HX, "Accept": "application/json"}
    assert web.post(f"/enquiries/{eid}/edit", headers=js, data={"source_url": "linkedin.com/x"}).status_code == 400
    assert web.post(f"/enquiries/{eid}/edit", headers=js, data={"email": "not-an-email"}).status_code == 400
    future = (local_today(get_settings().tz) + timedelta(days=2)).isoformat()
    assert web.post(f"/enquiries/{eid}/edit", headers=js, data={"date_posted": future}).status_code == 400
    frag = web.get(f"/enquiries/{eid}/edit-form?next=/inbox", headers=HX).text
    assert f'action="/enquiries/{eid}/edit"' in frag and "<html" not in frag
    page = web.get(f"/enquiries/{eid}/edit-form?next=//evil.example").text
    assert 'id="enquiry-edit-page"' in page
    # The edit page's own Back link and hidden next never carry the off-site target (the header's offering
    # filter echoes the request URL into its own next, which its POST re-validates).
    assert 'href="//evil.example"' not in page and 'name="next" value="//evil.example"' not in page
    inbox = web.get("/inbox?tab=accepted").text
    assert f'hx-get="/enquiries/{eid}/edit-form' in inbox


def test_missed_work_is_listed_in_the_ai_down_banner_and_on_health(web: TestClient) -> None:
    _add(web)
    health.note_failure("bad_response", CREDITS, "triage")
    page = unescape(web.get("/pipeline").text)
    assert 'id="ai-down-banner"' in page and 'id="ai-missed"' in page and "not read by the AI" in page
    hp = unescape(web.get("/health").text)
    assert "Not done: 1 enquiry not read by the AI" in hp


def test_edit_service_keeps_unchanged_fields(db_url: str) -> None:
    from tests.integration.web.conftest import make_enquiry, triaged

    eid = make_enquiry("P1", **triaged("genuine", "Buyer", summary="Keep me"))
    with session_scope() as db:
        enquiry_edit.edit(db, eid, fields={"city": "Delhi"})
        e = db.get(Enquiry, eid)
        assert e.city == "Delhi" and e.requirement_summary == "Keep me"


def test_switched_off_offerings_are_collapsed(db_url: str) -> None:
    from sales0.db.models import Offering

    with TestClient(create_app()) as c:
        with session_scope() as db:
            off = db.scalars(select(Offering)).first()
            off.active = False
            name = off.name
        html = unescape(c.get("/keywords?view=offering").text)
        assert 'id="kw-inactive-offerings"' in html
        lane = html.split('id="kw-inactive-offerings"', 1)[1]
        assert name in lane


def test_add_enquiry_with_a_working_ai_still_goes_to_the_company(db_url: str) -> None:
    set_llm_client(FakeLLMClient(responder=lambda req: triage_answer(company="Good Co", requirement_summary="Good Co - training")))
    try:
        with TestClient(create_app()) as c:
            r = c.post("/enquiries/add", data={"text": POST, "accept": "1"}, follow_redirects=False)
            with session_scope() as db:
                e = db.scalar(select(Enquiry))
                assert e.triage_status == "done" and r.headers["location"] == f"/companies/{e.company_id}"
            assert 'id="ai-missed-banner"' not in c.get("/pipeline").text
    finally:
        set_llm_client(None)
