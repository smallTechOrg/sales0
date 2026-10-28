"""The CRM through the REAL publishers: outreach approve/send (fake Gmail) and the reply poller."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.db.models import Enquiry, FollowUp, Lead
from sales0.services import replies
from sales0.services.crm import prospects
from sales0.services.outreach import messages
from tests.integration.crm import helpers as h
from tests.integration.outreach import conftest as _outreach
from tests.integration.outreach.conftest import Team, make_message
from tests.integration.replies.conftest import fresh, reply
from tests.support.fake_llm import FakeLLMClient

db = _outreach.db  # migrated SQLite + env for the team fixtures
team = _outreach.team


def _prospect_with_draft(db: Session, team: Team, *, email: str = "p@acme.example"):
    prof = h.profile(db)
    e = h.enquiry(db, profile_row=prof, company="Acme Training", email=email, contact_person="Priya")
    db.commit()
    v = prospects.ensure_for_enquiry(db, e.id)
    msg = make_message(db, team.ann, to=email)
    msg.enquiry_id, msg.prospect_id = e.id, v.id
    db.commit()
    return v, e, msg


def test_send_then_human_reply_walks_the_prospect_to_a_lead(db: Session, team: Team, installed) -> None:
    v, e, msg = _prospect_with_draft(db, team)
    # a draft or approved message changes nothing
    messages.approve(db, msg.id, team.ann)
    assert prospects.get_prospect(db, v.id).stage == "new"
    sent = messages.send(db, msg.id, team.ann)
    assert sent.state == "sent"
    p = prospects.get_prospect(db, v.id)
    assert p.stage == "contacted" and p.contacted_at is not None
    assert db.scalar(select(func.count(FollowUp.id)).where(FollowUp.prospect_id == v.id)) == 3
    db.refresh(e)
    assert e.date_email_sent is not None

    team.gmail[team.ann.id].threads[sent.gmail_thread_id] = team.gmail[team.ann.id].threads.get(sent.gmail_thread_id, [])
    reply(team, team.ann, sent, "p@acme.example", snippet="Yes, interested - can we talk Thursday?")
    replies.poll_all(llm=FakeLLMClient([{"kind": "human", "intent": "interested", "reason": "wants a call"}]))
    db.expire_all()
    assert fresh(db, sent).reply_status == "replied"
    p = prospects.get_prospect(db, v.id)
    assert p.status == "converted" and p.open_lead_id is not None and p.responded_at is not None
    lead = db.get(Lead, p.open_lead_id)
    assert lead.stage == "meeting_scheduled" and lead.opened_by == "reply"
    assert p.replies and p.replies[0].intent == "interested" and p.messages and p.messages[0].state == "sent"
    kinds = [t.kind for t in p.timeline]
    assert {"created", "enquiry_linked", "stage_change", "email_sent", "reply_received", "lead_created"} <= set(kinds)


def test_unsubscribe_reply_through_the_poller_flags_the_prospect(db: Session, team: Team, installed) -> None:
    v, e, msg = _prospect_with_draft(db, team)
    messages.approve(db, msg.id, team.ann)
    sent = messages.send(db, msg.id, team.ann)
    reply(team, team.ann, sent, "p@acme.example", snippet="Please remove me from your list")
    replies.poll_all(llm=FakeLLMClient([{"kind": "human", "intent": "unsubscribe", "reason": "asked to stop"}]))
    db.expire_all()
    p = prospects.get_prospect(db, v.id)
    assert [f.kind for f in p.flags] == ["no_follow_up"] and p.open_lead_id is None
    assert db.scalar(select(func.count(Lead.id))) == 0
    assert db.scalar(select(func.count(FollowUp.id)).where(FollowUp.cancelled_at.is_(None), FollowUp.done_at.is_(None))) == 0


def test_the_crm_not_installed_leaves_outreach_untouched(db: Session, team: Team) -> None:
    v, e, msg = _prospect_with_draft(db, team)
    messages.approve(db, msg.id, team.ann)
    assert messages.send(db, msg.id, team.ann).state == "sent"
    assert prospects.get_prospect(db, v.id).stage == "new"  # no subscriber, no effect, no error
    assert db.get(Enquiry, e.id) is not None
