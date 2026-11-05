"""q24: the automatic company status (one recalculation after every saved action, a timeline entry naming the trigger),
the manual override (holds until the next real outreach), the new lead stages, one board per company, closed companies
on no board (Companies -> Archived) and reopen -> Prospects / New."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import Company, Enquiry, Prospect, TimelineEvent
from sales0.services.crm import auto_status, company_pipeline, lifecycle, prospects, timeline
from sales0.services.crm.actor import Actor
from sales0.web.app import create_app
from tests.integration.crm import helpers as h
from tests.integration.web.conftest import HX

ANA = Actor("Ana", None)
DAY = timedelta(days=1)


@pytest.fixture
def auto(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SALES0_AUTO_STATUS", "true")
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def web(db_url: str, auto: None) -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def _manual(name: str = "Cold Co") -> tuple[int, int]:
    with session_scope() as db:
        v = prospects.create_manual(db, name="Asha Rao", company=name)
        return v.id, int(db.get(Prospect, v.id).company_id)


def _email(pid: int, at: datetime) -> None:
    with session_scope() as db:  # the commit runs the recalculation
        timeline.add_event(db, subject_type="prospect", subject_id=pid, kind="email_sent", body="Hello", actor=ANA, occurred_at=at)


def _co(cid: int) -> Company:
    with session_scope() as db:
        co = db.get(Company, cid)
        db.expunge(co)
        return co


def _auto_entries(cid: int) -> list[str]:
    with session_scope() as db:
        return [t.body for t in db.scalars(select(TimelineEvent).where(TimelineEvent.company_id == cid, TimelineEvent.subject_type == "company",
                                                                    TimelineEvent.body.like("Status updated automatically%")).order_by(TimelineEvent.id))]


def test_outreach_days_move_a_prospect_forward_and_the_timeline_names_the_trigger(db_url: str, auto: None) -> None:
    pid, cid = _manual()
    t0 = datetime.now(UTC) - 5 * DAY
    _email(pid, t0)
    assert (_co(cid).stage, _co(cid).sub_status) == ("prospect", "contacted")
    _email(pid, t0 + timedelta(minutes=5))  # same day: one touch
    assert _co(cid).sub_status == "contacted"
    _email(pid, t0 + 2 * DAY)
    assert _co(cid).sub_status == "follow_up_1"
    entries = _auto_entries(cid)
    assert len(entries) == 2 and all("(triggered by: Email sent)" in e for e in entries)
    assert "Prospect / New -> Prospect / Contacted" in entries[0]


def test_a_note_does_not_move_it_and_auto_status_off_moves_nothing(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    pid, cid = _manual()
    with session_scope() as db:
        timeline.add_note(db, subject_type="prospect", subject_id=pid, body="Their CFO is away", actor=ANA)
    assert _co(cid).sub_status == "new"
    _email(pid, datetime.now(UTC))  # SALES0_AUTO_STATUS=false in the test env: legacy behaviour
    assert _co(cid).sub_status == "new" and not _auto_entries(cid)


def test_a_logged_meeting_makes_a_prospect_a_lead(db_url: str, auto: None) -> None:
    pid, cid = _manual()
    with session_scope() as db:
        timeline.add_event(db, subject_type="prospect", subject_id=pid, kind="meeting", body="Intro call booked", actor=ANA)
    co = _co(cid)
    assert (co.stage, co.sub_status) == ("lead", "meeting_scheduled")
    assert "(triggered by: Meeting logged)" in _auto_entries(cid)[-1]


def test_manual_override_holds_until_the_next_real_outreach(web: TestClient) -> None:
    pid, cid = _manual()
    _email(pid, datetime.now(UTC) - 6 * DAY)
    _email(pid, datetime.now(UTC) - 4 * DAY)
    assert _co(cid).sub_status == "follow_up_1"
    r = web.post(f"/companies/{cid}/move", headers=HX, data={"target": "prospect:contacted", "why": "wrong count"})
    assert r.status_code == 204
    co = _co(cid)
    assert co.sub_status == "contacted" and co.status_override_at is not None and co.status_override_sub == "contacted"
    with session_scope() as db:  # a note is not outreach: the manual status holds
        timeline.add_note(db, subject_type="prospect", subject_id=pid, body="checking in", actor=ANA)
        assert auto_status.override_active(db, db.get(Company, cid))
    assert _co(cid).sub_status == "contacted"
    _email(pid, datetime.now(UTC) + timedelta(seconds=5))  # outreach after the override: counts from Contacted
    assert _co(cid).sub_status == "follow_up_1"
    page = unescape(web.get(f"/companies/{cid}").text)
    assert 'id="status-menu"' in page and "Automatic status" in page and "What moves it:" in page


def test_lead_stages_and_the_order(web: TestClient) -> None:
    _pid, cid = _manual()
    for target in ("lead:meeting_scheduled", "lead:assessment", "lead:proposal_sent", "lead:interviews", "lead:on_hold"):
        assert web.post(f"/companies/{cid}/move", headers=HX, data={"target": target}).status_code == 204
        assert (_co(cid).stage, _co(cid).sub_status) == ("lead", target.split(":")[1])
    with session_scope() as db:
        board = company_pipeline.tab_board(db, "leads")
        assert [c.key for c in board.columns] == ["meeting_scheduled", "assessment", "proposal_sent", "interviews", "on_hold"]
        assert [c.total for c in board.columns] == [0, 0, 0, 0, 1]
    r = web.post(f"/companies/{cid}/move", headers=HX, data={"target": "prospect:new"})  # no way back to Prospects
    assert "cannot go back" in unescape(r.text) and _co(cid).stage == "lead"


def test_one_board_per_company_closed_on_none_and_reopen_returns_to_prospects_new(web: TestClient) -> None:
    with session_scope() as db:
        e = h.enquiry(db, profile_row=h.profile(db, slug="in/board-1"), company="Inbound Ltd")
        prospects.ensure_for_enquiry(db, e.id)
        cid = int(db.get(Enquiry, e.id).company_id)
    _pid2, cold = _manual("Cold Co")

    def where(company_id: int) -> list[str]:
        with session_scope() as db:
            out = []
            eb = company_pipeline.enquiry_board(db)
            if any(c.company_id == company_id for col in eb.columns for c in col.cards):
                out.append("enquiries")
            for tab in ("prospects", "leads"):
                if any(c.id == company_id for col in company_pipeline.tab_board(db, tab).columns for c in col.cards):
                    out.append(tab)
            return out

    assert where(cid) == ["enquiries"] and where(cold) == ["prospects"]
    r = web.post("/pipeline/move", headers=HX, data={"company_id": str(cid), "target": "prospect:contacted", "next": "/pipeline?tab=enquiries"})
    assert r.status_code == 200 and "Inbound Ltd: Prospect: Contacted." in unescape(r.text) and 'id="pipeline-body"' in r.text
    assert where(cid) == ["prospects"]
    r = web.post("/pipeline/move", headers=HX, data={"company_id": str(cid), "target": "lead:meeting_scheduled",
                                                     "meeting_at": "2026-12-01T10:30", "next": "/pipeline?tab=prospects"})
    assert r.status_code == 200 and where(cid) == ["leads"]
    with session_scope() as db:
        assert lifecycle.life_view(db, cid).lead_meeting_at is not None
    web.post("/pipeline/move", headers=HX, data={"company_id": str(cid), "target": "closed:not_interested", "why": "budget frozen",
                                                 "next": "/pipeline?tab=leads"})
    co = _co(cid)
    assert co.off_ramp == "not_interested" and co.off_ramp_reason == "budget frozen" and where(cid) == []
    archived = unescape(web.get("/companies?view=archived").text)
    assert "Inbound Ltd" in archived and "Cold Co" not in archived and f'id="reopen-{cid}"' in archived and ">Closed<" in archived
    assert web.post(f"/companies/{cid}/reopen", headers=HX).status_code == 204
    co = _co(cid)
    assert (co.stage, co.sub_status, co.off_ramp) == ("prospect", "new", None)
    assert where(cid) == ["enquiries"]  # an inbound company nobody has contacted in this cycle


def test_pipeline_board_has_drag_targets_and_the_move_popup(web: TestClient) -> None:
    _manual()
    html = web.get("/pipeline?tab=prospects").text
    assert 'id="move-dialog"' in html and 'data-drop-target="prospect:follow_up_1"' in html and 'draggable="true"' in html
    assert 'data-drop-target="closed:not_interested"' in html and 'id="lane-became_lead"' not in html and 'id="closed-lane"' not in html
    assert 'name="meeting_at"' in html and "data-move-select" in html
