"""E1: an outreach email sent for a prospect in stage ``new`` moves it to ``contacted`` and schedules the three
follow-ups, on EVERY send path (real ``messages.send`` / ``retry_send`` with a fake Gmail)."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.clock import local_today
from sales0.config import get_settings
from sales0.db.models import Enquiry, FollowUp, Prospect, StatusHistory, TimelineEvent
from sales0.services.crm import contacts, prospects
from sales0.services.crm.actor import SYSTEM
from sales0.services.outreach import messages
from tests.integration.crm import helpers as h
from tests.integration.outreach import conftest as _outreach
from tests.integration.outreach.conftest import Team, make_message

db = _outreach.db
team = _outreach.team


def _today() -> date:
    return local_today(get_settings().tz)


def _send(db: Session, team: Team, msg) -> None:  # type: ignore[no-untyped-def]
    messages.approve(db, msg.id, team.ann)
    sent = messages.send(db, msg.id, team.ann)
    assert sent.state == "sent"


def _opens(db: Session, pid: int) -> list[FollowUp]:
    return list(db.scalars(select(FollowUp).where(FollowUp.prospect_id == pid, FollowUp.done_at.is_(None), FollowUp.cancelled_at.is_(None)).order_by(FollowUp.due_on)))


def _assert_contacted(db: Session, pid: int) -> Prospect:
    db.expire_all()
    p = db.get(Prospect, pid)
    assert p.stage == "contacted" and p.contacted_at is not None and not p.contacted_estimated
    assert [f.kind for f in _opens(db, pid)] == ["follow_up_1", "follow_up_2", "final_follow_up"]
    t = [t for t in db.scalars(select(TimelineEvent).where(TimelineEvent.prospect_id == pid)) if t.kind == "stage_change"]
    assert any((x.data or {}).get("to") == "contacted" and (x.data or {}).get("channel") == "email" for x in t)
    return p


def test_outreach_send_for_a_new_prospect_with_a_direct_link(db: Session, team: Team, installed) -> None:
    p = prospects.create_manual(db, name="Direct Co", company="Direct Co", email="d@direct.example")
    msg = make_message(db, team.ann, to="d@direct.example")
    msg.prospect_id = p.id
    db.commit()
    _send(db, team, msg)
    got = _assert_contacted(db, p.id)
    assert (got.next_follow_up_on - _today()).days == get_settings().followup_days_1


def test_resend_after_unknown_outcome_also_moves_new_to_contacted(db: Session, team: Team, installed) -> None:
    p = prospects.create_manual(db, name="Retry Co", email="r@retry.example")
    msg = make_message(db, team.ann, to="r@retry.example")
    msg.prospect_id = p.id
    db.commit()
    messages.approve(db, msg.id, team.ann)
    team.gmail[team.ann.id].send_draft_error = None
    gm = team.gmail[team.ann.id]
    real = gm.send_draft

    def boom(approved):  # type: ignore[no-untyped-def]
        gm.send_draft = real  # only the first attempt fails
        raise TimeoutError("network")

    gm.send_draft = boom  # type: ignore[method-assign]
    failed = messages.send(db, msg.id, team.ann)
    assert failed.state == "failed" and failed.send_outcome_unknown
    assert db.get(Prospect, p.id).stage == "new"  # nothing sent, nothing recorded
    done = messages.retry_send(db, msg.id, team.ann, True)
    assert done.state == "sent"
    _assert_contacted(db, p.id)


def test_sheet_imported_prospect_with_an_estimated_contact_date_still_gets_reminders(db: Session, team: Team, installed) -> None:
    """A sheet row (stage new, contact date only an estimate): the first real email replaces the estimate AND
    schedules the follow-ups (the estimate used to hide the 'first contact')."""
    p = prospects.create_manual(db, name="Sheet Co", email="s@sheet.example", source="sheet")
    row = db.get(Prospect, p.id)
    row.stage_source, row.contacted_at, row.contacted_estimated = "sheet", h.T0 - timedelta(days=40), True
    msg = make_message(db, team.ann, to="s@sheet.example")
    msg.prospect_id = p.id
    db.commit()
    _send(db, team, msg)
    got = _assert_contacted(db, p.id)
    assert got.stage_source == "app"


def test_prospect_reached_through_a_contact_link(db: Session, team: Team, installed) -> None:
    p = prospects.create_manual(db, name="Linked Co", company="Linked Co")
    c, _ = contacts.upsert_row(db, full_name="Lena", email="lena@linked.example", company_id=db.get(Prospect, p.id).company_id,
                               origin="manual", actor=SYSTEM)
    contacts.link(db, c.id, "prospect", p.id, "buyer")
    msg = make_message(db, team.ann, to="lena@linked.example")  # no prospect_id, no enquiry
    db.commit()
    _send(db, team, msg)
    _assert_contacted(db, p.id)
    db.refresh(msg)
    assert msg.prospect_id == p.id


def test_prospect_reached_through_the_company_link(db: Session, team: Team, installed) -> None:
    p = prospects.create_manual(db, name="Comp Person", company="Companyco")
    msg = make_message(db, team.ann, to="someone.else@elsewhere.example")
    msg.company_id = db.get(Prospect, p.id).company_id  # drafted from the company page
    db.commit()
    _send(db, team, msg)
    _assert_contacted(db, p.id)


def test_two_open_prospects_in_the_company_are_not_guessed(db: Session, team: Team, installed) -> None:
    a = prospects.create_manual(db, name="A Person", company="Twin Co")
    prospects.create_manual(db, name="B Person", company="Twin Co")
    msg = make_message(db, team.ann, to="x@elsewhere.example")
    msg.company_id = db.get(Prospect, a.id).company_id
    db.commit()
    _send(db, team, msg)
    assert db.get(Prospect, a.id).stage == "new"


def test_enquiry_linked_outreach_without_a_prospect_ensures_one(db: Session, team: Team, installed) -> None:
    e = h.enquiry(db, company="No Prospect Yet", email="n@np.example")  # accepted, prospect_id NULL
    msg = make_message(db, team.ann, to="n@np.example")
    msg.enquiry_id = e.id
    db.commit()
    assert e.prospect_id is None
    _send(db, team, msg)
    db.refresh(e)
    assert e.prospect_id is not None
    _assert_contacted(db, e.prospect_id)
    # status history of the enquiry records the first-email date
    hist = list(db.scalars(select(StatusHistory).where(StatusHistory.entity_type == "enquiry", StatusHistory.entity_id == e.id)))
    assert any(x.field == "date_email_sent" for x in hist)


def test_enquiry_still_pending_review_leaves_a_visible_note_instead_of_silence(db: Session, team: Team, installed) -> None:
    e = h.enquiry(db, status="pending_review", company="Unreviewed Co", email="u@un.example")
    msg = make_message(db, team.ann, to="u@un.example")
    msg.enquiry_id = e.id
    db.commit()
    _send(db, team, msg)
    notes = [t for t in db.scalars(select(TimelineEvent).where(TimelineEvent.subject_type == "enquiry", TimelineEvent.subject_id == e.id))]
    assert any("not been accepted" in (t.body or "") for t in notes)
    assert db.get(Enquiry, e.id).prospect_id is None


def test_planned_dates_from_log_contact_are_honoured_and_not_duplicated(db: Session, team: Team, installed) -> None:
    """Log contact (WhatsApp) with planned dates, then an email goes out: the planned dates stay, no second set."""
    p = prospects.create_manual(db, name="Planned Co", email="pl@planned.example")
    d = _today()
    dues = {"follow_up_1": d + timedelta(days=2), "follow_up_2": d + timedelta(days=5), "final_follow_up": d + timedelta(days=9)}
    prospects.log_contact(db, p.id, channel="whatsapp", due_dates=dues)
    msg = make_message(db, team.ann, to="pl@planned.example")
    msg.prospect_id = p.id
    db.commit()
    _send(db, team, msg)
    opens = _opens(db, p.id)
    assert [f.due_on for f in opens] == [dues["follow_up_1"], dues["follow_up_2"], dues["final_follow_up"]]
    assert len(list(db.scalars(select(FollowUp).where(FollowUp.prospect_id == p.id)))) == 3
