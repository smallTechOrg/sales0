"""E2 through the real app: the outbox panel + "Draft due follow-ups", the per-follow-up "Draft email", the planned channel
in the Log contact form / follow-up row, and the prospect page. Gmail + LLM are fakes; nothing is ever sent."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.clock import local_today, utcnow
from sales0.config import get_settings
from sales0.db.engine import session_scope
from sales0.db.models import FollowUp, OutreachMessage
from sales0.llm import set_llm_client
from sales0.services import suppressions
from sales0.services.crm import prospects
from sales0.web.app import create_app
from tests.integration.crm import conftest as _crm
from tests.integration.outreach import conftest as _outreach
from tests.integration.outreach.conftest import Team, make_message
from tests.integration.web.conftest import HX
from tests.support.fake_llm import FakeLLMClient

db = _outreach.db
team = _outreach.team
installed = _crm.installed

JSON = {"Accept": "application/json"}
REPLY = {"subject": "x", "body": "Hello Priya,\n\nA quick follow-up on my earlier note to Due Co.\n\nBest regards,\nAnn Rao"}


@pytest.fixture
def web(team: Team, installed) -> Iterator[TestClient]:
    set_llm_client(FakeLLMClient(responder=lambda req: dict(REPLY)))
    with TestClient(create_app()) as c:
        assert c.post("/team/acting", data={"member_id": team.ann.id, "next": "/outreach"}, follow_redirects=False).status_code == 303
        yield c


def _overdue_prospect(team: Team, name: str = "Due Co", email: str = "due@due.example") -> int:
    """Contacted by email 10 days ago (the thread exists), so every reminder through +7 is overdue."""
    today = local_today(get_settings().tz)
    with session_scope() as db:
        p = prospects.create_manual(db, name=name, company=name, email=email)
        prospects.log_contact(db, p.id, channel="email", contacted_on=today - timedelta(days=10))
        m = make_message(db, team.ann, to=email, subject=f"Workshops for {name}", state="sent")
        m.sent_at, m.prospect_id, m.gmail_thread_id, m.gmail_message_id = utcnow() - timedelta(days=10), p.id, "thr-x", "g-x"
        db.commit()
        return p.id


def test_outbox_lists_due_follow_ups_and_drafts_them_on_click_without_sending(web: TestClient, team: Team) -> None:
    pid = _overdue_prospect(team)
    html = web.get("/outreach").text
    assert 'id="due-followups"' in html and "Due Co" in html and "ready to draft" in html and "Draft due follow-ups (1)" in html
    r = web.post("/pipeline/followups/draft-due", data={"next": "/outreach"}, headers=HX)
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/outreach"
    with session_scope() as db:
        drafts = list(db.scalars(select(OutreachMessage).where(OutreachMessage.follow_up_id.is_not(None))))
        assert len(drafts) == 1 and drafts[0].state == "draft" and drafts[0].prospect_id == pid
    assert not team.gmail[team.ann.id].sent
    after = web.get("/outreach").text
    assert "a draft already exists" in after and "Draft due follow-ups" in after
    # the draft is reviewed on its message page like any other
    assert web.get(f"/outreach/messages/{drafts[0].id}").status_code == 200


def test_batch_json_reports_drafted_skipped_and_remaining(web: TestClient, team: Team) -> None:
    _overdue_prospect(team, "Ok Co", "ok@ok.example")
    _overdue_prospect(team, "Stop Co", "stop@stop.example")
    with session_scope() as db:
        suppressions.add(db, "stop@stop.example", "unsubscribe", note="asked")
    r = web.post("/pipeline/followups/draft-due", data={"next": "/outreach"}, headers=JSON)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["drafted"] == 1 and data["skipped"] >= 1 and data["remaining"] == 0
    reasons = [o["reason"] for o in data["outcomes"] if o["status"] == "skipped"]
    assert any("suppression" in (x or "") for x in reasons)


def test_prospect_page_shows_the_draft_button_the_reason_and_the_channel_control(web: TestClient, team: Team) -> None:
    pid = _overdue_prospect(team)
    html = web.get(f"/pipeline/prospects/{pid}").text
    assert "Draft email" in html and 'name="channel"' in html and 'id="draft-due-prospect-form"' in html
    with session_scope() as db:
        fu1 = db.scalar(select(FollowUp.id).where(FollowUp.prospect_id == pid, FollowUp.kind == "follow_up_1"))
        fu2 = db.scalar(select(FollowUp.id).where(FollowUp.prospect_id == pid, FollowUp.kind == "follow_up_2"))
    assert "Follow-up 1 comes first" in html  # one at a time
    # per-follow-up draft
    r = web.post(f"/pipeline/followups/{fu1}/draft", data={}, headers=HX)
    assert r.status_code == 204 and "/outreach/messages/" in r.headers["HX-Redirect"]
    # a second click: nothing new, a clear reason
    again = web.post(f"/pipeline/followups/{fu1}/draft", data={}, headers=HX)
    assert "a draft already exists" in again.text
    page = web.get(f"/pipeline/prospects/{pid}").text
    assert "Review the email draft" in page
    # planned channel: switch Follow-up 2 to WhatsApp -> no email draft offered
    ch = web.post(f"/pipeline/followups/{fu2}/channel", data={"channel": "whatsapp"}, headers=HX)
    assert ch.status_code == 200 and "WhatsApp" in ch.text
    with session_scope() as db:
        assert db.get(FollowUp, fu2).planned_channel == "whatsapp"
    assert web.post(f"/pipeline/followups/{fu2}/channel", data={"channel": "pigeon"}, headers=HX).status_code == 200  # toast error
    with session_scope() as db:
        assert db.get(FollowUp, fu2).planned_channel == "whatsapp"


def test_log_contact_form_has_a_planned_channel_per_follow_up_and_stores_it(web: TestClient, team: Team) -> None:
    with session_scope() as db:
        pid = prospects.create_manual(db, name="Plan Co", email="plan@plan.example").id
    html = web.get(f"/pipeline/prospects/{pid}").text
    for kind in ("follow_up_1", "follow_up_2", "final_follow_up"):
        assert f'name="channel_{kind}"' in html
    r = web.post(f"/pipeline/prospects/{pid}/contact", data={"channel": "whatsapp", "channel_follow_up_1": "linkedin",
                                                          "channel_follow_up_2": "email", "channel_final_follow_up": "call"}, headers=HX)
    assert r.status_code == 200
    with session_scope() as db:
        got = {f.kind: f.planned_channel for f in db.scalars(select(FollowUp).where(FollowUp.prospect_id == pid))}
    assert got == {"follow_up_1": "linkedin", "follow_up_2": "email", "final_follow_up": "call"}


def test_due_panel_has_the_batch_button_and_per_row_draft(web: TestClient, team: Team) -> None:
    _overdue_prospect(team)
    html = web.get("/pipeline").text
    assert 'id="draft-due-panel-form"' in html and "Draft email" in html
