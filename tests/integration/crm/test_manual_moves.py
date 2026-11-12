"""q25: no automation moves a company between statuses/boards (only a person does); a company marked Client by
mistake can be put back on Leads or Prospects as if it never was a client; Add enquiry (paste a post)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from html import unescape
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sales0.db.engine import session_scope
from sales0.db.models import Client, Company, Enquiry, FollowUp, Lead, Prospect, TimelineEvent
from sales0.llm import set_llm_client
from sales0.llm.client import StructuredRequest
from sales0.services.crm import deals, followups, lifecycle, prospects, timeline
from sales0.services.crm.actor import Actor
from sales0.web.app import create_app
from tests.integration.crm import helpers as h
from tests.integration.web.conftest import HX
from tests.support.fake_llm import FakeLLMClient, triage_answer

ANA = Actor("Ana", None)


@pytest.fixture
def web(db_url: str) -> Iterator[TestClient]:
    set_llm_client(FakeLLMClient(responder=lambda req: triage_answer()))
    with TestClient(create_app()) as c:
        yield c
    set_llm_client(None)


def _inbound(slug: str = "in/m-1", company: str = "Inbound Ltd") -> tuple[int, int, int]:
    with session_scope() as db:
        e = h.enquiry(db, profile_row=h.profile(db, slug=slug), company=company)
        v = prospects.ensure_for_enquiry(db, e.id)
        return e.id, v.id, int(db.get(Enquiry, e.id).company_id)


def _co(cid: int) -> tuple[str | None, str | None, str | None]:
    with session_scope() as db:
        co = db.get(Company, cid)
        return co.stage, co.sub_status, co.off_ramp


def test_nothing_moves_a_company_by_itself(db_url: str) -> None:
    eid, pid, cid = _inbound()
    with session_scope() as db:  # logging the first contact by hand: recorded, the status stays
        prospects.log_contact(db, pid, channel="call", note="spoke to them")
        assert db.get(Prospect, pid).contacted_at is not None
    assert _co(cid) == ("prospect", "new", None)
    with session_scope() as db:  # an email goes out
        prospects.record_email_contact(db, pid, when=datetime.now(UTC), actor=ANA)
        timeline.add_event(db, subject_type="prospect", subject_id=pid, kind="email_sent", body="Hello", actor=ANA)
    assert _co(cid) == ("prospect", "new", None)
    with session_scope() as db:  # the scheduled Follow-up 1 reminder marked done
        fid = db.scalar(select(FollowUp.id).where(FollowUp.prospect_id == pid, FollowUp.kind == "follow_up_1"))
        assert fid is not None
        followups.complete(db, fid, actor=ANA, channel="call")
    assert _co(cid) == ("prospect", "new", None)
    with session_scope() as db:  # enquiry statuses: the enquiry changes, the company does not
        deals.set_status(db, eid, "follow_up_2", actor=ANA)
        deals.set_status(db, eid, "won", actor=ANA)
        assert db.get(Enquiry, eid).deal_status == "won"
        co = db.get(Company, cid)
        assert lifecycle.active_suggestion(co)["kind"] == "client"  # a hint; a person decides
    assert _co(cid) == ("prospect", "new", None)
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(Client)) == 0
        lifecycle.accept_suggestion(db, cid, actor=ANA)  # the person accepts the hint
    assert _co(cid)[0] == "client"


@pytest.mark.parametrize("target", ["lead:proposal_sent", "prospect:follow_up_1"])
def test_a_client_marked_by_mistake_goes_back_as_if_it_never_was(web: TestClient, target: str) -> None:
    eid, pid, cid = _inbound()
    with session_scope() as db:
        deals.set_status(db, eid, "won", actor=ANA)
        lifecycle.mark_client(db, cid, actor=ANA)
    assert _co(cid)[0] == "client"
    page = unescape(web.get(f"/companies/{cid}").text)
    assert 'id="undo-client-hint"' in page and 'value="lead:meeting_scheduled"' in page and 'value="prospect:new"' in page
    r = web.post(f"/companies/{cid}/move", headers=HX, data={"target": target, "why": "clicked the wrong button"})
    assert r.status_code == 204
    kind, sub = target.split(":")
    assert _co(cid) == (kind, sub, None)
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(Client)) == 0  # no client record left behind
        assert db.scalar(select(func.count()).select_from(Lead).where(Lead.stage == "won")) == 0
        assert db.get(Enquiry, eid).deal_status == sub
        body = db.scalar(select(TimelineEvent.body).where(TimelineEvent.company_id == cid, TimelineEvent.body.like("Client status undone%")))
        assert body and "clicked the wrong button" in body
        if kind == "lead":
            assert db.scalar(select(func.count()).select_from(Lead).where(Lead.closed_at.is_(None))) == 1
        else:
            assert db.get(Prospect, pid).status == "open"
    board = web.get(f"/pipeline?tab={'leads' if kind == 'lead' else 'prospects'}").text
    assert f'id="company-{cid}"' in board


def test_add_enquiry_by_pasting_a_post(web: TestClient) -> None:
    def answer(req: StructuredRequest) -> Any:
        return triage_answer(company="Pasted Logistics", contact_person="Ravi Kumar", email="ravi@pasted.example",
                             requirement_summary="Pasted Logistics - GenAI workshop for 30 managers - Pune")

    set_llm_client(FakeLLMClient(responder=answer))
    page = web.get("/pipeline?tab=enquiries").text
    assert 'id="add-enquiry"' in page and 'action="/enquiries/add"' in page
    assert 'id="add-enquiry"' in web.get("/inbox").text
    text = "Hiring a GenAI trainer: Pasted Logistics needs a two-day GenAI workshop for 30 managers in Pune next month. Write to ravi@pasted.example."
    r = web.post("/enquiries/add", data={"text": text, "author": "Ravi Kumar", "accept": "1"}, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as db:
        e = db.scalar(select(Enquiry))
        assert e.origin == "manual" and e.review_status == "accepted" and e.company_id is not None
        assert e.requirement_summary.startswith("Pasted Logistics") and e.email == "ravi@pasted.example"
        cid = e.company_id
    assert r.headers["location"] == f"/companies/{cid}"
    assert _co(cid) == ("prospect", "new", None)
    assert 'id="enquiry-' in web.get("/pipeline?tab=enquiries").text
    again = web.post("/enquiries/add", data={"text": text, "accept": "1"}, follow_redirects=False)
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(Enquiry)) == 1  # deduped
    assert again.headers["location"] == f"/companies/{cid}"
    # unticked: waits in the Inbox
    web.post("/enquiries/add", data={"text": "Another company, Beta Foods, wants an AI literacy session for 100 staff in Mumbai."})
    with session_scope() as db:
        assert db.scalar(select(Enquiry.review_status).where(Enquiry.id != e.id)) == "pending_review"
    assert web.post("/enquiries/add", data={"text": "too short"}, headers=HX).status_code in (200, 400)
