"""CRM hooks driven through ``events.publish`` (the way the publishers call them)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from sales0.db.models import Enquiry, FollowUp, Lead, OutreachMessage, Prospect
from sales0.services import events, inbox
from sales0.services.crm import flags, followups, hooks, prospects
from tests.integration.crm import helpers as h

REPLY_AT = h.T0 + timedelta(days=1)


def _accepted(db, company="Acme Training", email="asha@acme.example", **kw):
    e = h.enquiry(db, profile_row=h.profile(db, slug=f"in/hk{h._ids.__next__()}"), company=company, email=email, **kw)
    v = prospects.ensure_for_enquiry(db, e.id)
    return v, e


def _sent(db, m, e, **kw):
    msg = h.sent_message(db, m, enquiry_row=e, **kw)
    events.publish(db, events.MessageSent(outreach_message_id=msg.id, member_id=m.id, to_addrs=("asha@acme.example",), sent_at=h.T0))
    return msg


def _reply(db, m, msg, *, kind="human", intent="interested"):
    r = h.reply(db, msg, kind=kind, intent=intent, at=REPLY_AT)
    events.publish(db, events.ReplyReceived(outreach_message_id=msg.id, kind=kind, intent=intent, reply_id=r.id, member_id=m.id,
                                            from_addr=r.from_addr))
    return r


def test_install_is_idempotent_and_subscribes_its_events(db, installed) -> None:
    from sales0.services import crm

    crm.install()
    crm.install()
    assert len(events.subscribers(events.MessageSent)) == 1
    assert len(events.subscribers(events.ReplyReceived)) == 1
    assert len(events.subscribers(events.EnquiryDecided)) == 1
    assert len(events.subscribers(events.TrackedProfileHit)) == 1


# ---------------------------------------------------------------- enquiry decided
def test_accept_decision_creates_the_prospect(db, installed) -> None:
    prof = h.profile(db)
    e = h.enquiry(db, status="pending_review", profile_row=prof, company="Acme Training")
    out = inbox.decide(db, [e.id], "accept", None, None)
    assert out
    db.refresh(e)
    assert e.prospect_id is not None and db.get(Prospect, e.prospect_id).stage == "new"


def test_accept_with_a_member_records_the_member_as_actor(db, installed) -> None:
    m = h.member(db, "Priya")
    e = h.enquiry(db, status="pending_review", company="Acme Training")
    inbox.decide(db, [e.id], "accept", None, None, actor_member_id=m.id)
    db.refresh(e)
    p = db.get(Prospect, e.prospect_id)
    assert p.actor == "Priya" and p.actor_member_id == m.id


def test_discard_closes_a_new_prospect_only_when_no_other_accepted_enquiry(db, installed) -> None:
    prof = h.profile(db)
    a = h.enquiry(db, status="pending_review", profile_row=prof, company="Acme")
    b = h.enquiry(db, status="pending_review", profile_row=prof, company="Acme")
    inbox.decide(db, [a.id, b.id], "accept", None, None)
    db.refresh(a)
    pid = a.prospect_id
    inbox.decide(db, [a.id], "discard", "irrelevant", None)
    assert db.get(Prospect, pid).status == "open"  # b is still accepted
    inbox.decide(db, [b.id], "discard", "irrelevant", None)
    p = db.get(Prospect, pid)
    assert p.status == "closed" and p.closed_reason == "enquiry_discarded"
    inbox.decide(db, [b.id], "accept", None, None)  # re-accept reopens
    assert db.get(Prospect, pid).status == "open"


def test_discard_does_not_close_a_prospect_that_moved_on_or_is_sheet_staged(db, installed) -> None:
    v, e = _accepted(db)
    prospects.move_stage(db, v.id, "contacted", now=h.T0)
    e.review_status = "discarded"
    events.publish(db, events.EnquiryDecided(enquiry_id=e.id, decision="discard", previous_review_status="accepted"))
    assert db.get(Prospect, v.id).status == "open"
    v2, e2 = _accepted(db)
    db.get(Prospect, v2.id).stage_source = "sheet"
    e2.review_status = "discarded"
    events.publish(db, events.EnquiryDecided(enquiry_id=e2.id, decision="discard", previous_review_status="accepted"))
    assert db.get(Prospect, v2.id).status == "open"


# ---------------------------------------------------------------- message sent
def test_sent_message_marks_contacted_and_schedules_three_reminders(db, installed) -> None:
    m = h.member(db)
    v, e = _accepted(db)
    msg = _sent(db, m, e)
    p = prospects.get_prospect(db, v.id)
    assert p.stage == "contacted" and p.contacted_at == h.T0 and not p.contacted_estimated
    assert sorted(f.due_on for f in db.scalars(select(FollowUp).where(FollowUp.prospect_id == v.id))) == [h.day(3), h.day(7), h.day(14)]
    assert "email_sent" in [t.kind for t in p.timeline]
    sent_ev = next(t for t in p.timeline if t.kind == "email_sent")
    assert sent_ev.data["message_id"] == msg.id and sent_ev.body == "Corporate AI training for Acme"
    db.refresh(e)
    assert e.date_email_sent == h.day(0)
    db.refresh(msg)
    assert msg.prospect_id == v.id  # the message learned its prospect
    assert any(c.email == "asha@acme.example" for c in p.contacts)


def test_unsent_message_changes_nothing(db, installed) -> None:
    m = h.member(db)
    v, e = _accepted(db)
    for state in ("draft", "approved", "failed"):
        msg = h.sent_message(db, m, enquiry_row=e, state=state)
        events.publish(db, events.MessageSent(outreach_message_id=msg.id, member_id=m.id, to_addrs=("asha@acme.example",), sent_at=h.T0))
    assert prospects.get_prospect(db, v.id).stage == "new"
    assert db.scalar(select(func.count(FollowUp.id))) == 0


def test_second_publish_of_the_same_message_is_idempotent(db, installed) -> None:
    m = h.member(db)
    v, e = _accepted(db)
    msg = _sent(db, m, e)
    events.publish(db, events.MessageSent(outreach_message_id=msg.id, member_id=m.id, to_addrs=(), sent_at=h.T0))
    ks = [t.kind for t in prospects.get_prospect(db, v.id).timeline]
    assert ks.count("email_sent") == 1 and db.scalar(select(func.count(FollowUp.id))) == 3


def test_a_follow_up_send_completes_the_earliest_reminder_and_advances_the_stage(db, installed) -> None:
    m = h.member(db)
    v, e = _accepted(db)
    first = _sent(db, m, e)
    fu = h.sent_message(db, m, enquiry_row=e, sent_at=h.T0 + timedelta(days=4), follow_up_of_id=first.id)
    events.publish(db, events.MessageSent(outreach_message_id=fu.id, member_id=m.id, to_addrs=("asha@acme.example",), sent_at=fu.sent_at))
    p = prospects.get_prospect(db, v.id)
    assert p.stage == "follow_up_1" and p.next_follow_up_on == h.day(7)
    fu2 = h.sent_message(db, m, enquiry_row=e, sent_at=h.T0 + timedelta(days=8))
    events.publish(db, events.MessageSent(outreach_message_id=fu2.id, member_id=m.id, to_addrs=(), sent_at=fu2.sent_at))
    assert prospects.get_prospect(db, v.id).stage == "follow_up_2"


def test_an_accepted_enquiry_without_a_prospect_gets_one_on_send(db, installed) -> None:
    m = h.member(db)
    e = h.enquiry(db, company="Late Accept Co")  # accepted before the CRM existed
    assert e.prospect_id is None
    _sent(db, m, e)
    db.refresh(e)
    assert e.prospect_id is not None and db.get(Prospect, e.prospect_id).stage == "contacted"


def test_a_message_that_resolves_to_no_prospect_is_ignored(db, installed) -> None:
    m = h.member(db)
    msg = h.sent_message(db, m, to="stranger@nowhere.example")
    events.publish(db, events.MessageSent(outreach_message_id=msg.id, member_id=m.id, to_addrs=("stranger@nowhere.example",), sent_at=h.T0))
    assert db.scalar(select(func.count(Prospect.id))) == 0


def test_send_to_a_closed_prospect_records_the_contact_but_keeps_it_closed(db, installed) -> None:
    m = h.member(db)
    v, e = _accepted(db)
    prospects.close(db, v.id, "duplicate")
    _sent(db, m, e)
    p = prospects.get_prospect(db, v.id)
    assert p.status == "closed" and p.contacted_at is not None and p.stage == "contacted"
    assert "email_sent" in [t.kind for t in p.timeline]
    assert not [f for f in p.follow_ups if not f.done]  # a closed prospect gets no reminders


# ---------------------------------------------------------------- reply received
def _contacted(db):
    m = h.member(db)
    v, e = _accepted(db)
    msg = _sent(db, m, e)
    return m, v, msg


def test_interested_reply_to_a_contacted_prospect_only_suggests_a_lead(db, installed) -> None:
    from sales0.db.models import Company
    from sales0.services.crm import lifecycle

    m, v, msg = _contacted(db)
    r = _reply(db, m, msg)
    p = prospects.get_prospect(db, v.id)
    assert p.status == "open" and p.responded_at == REPLY_AT  # the app never moves the company by itself
    assert db.scalar(select(Lead).where(Lead.prospect_id == v.id)) is None
    co = db.get(Company, p.company_id)
    assert co.stage == "prospect" and lifecycle.active_suggestion(co)["kind"] == "reply"
    reply_ev = [t for t in p.timeline if t.kind == "reply_received"]
    assert len(reply_ev) == 1 and reply_ev[0].data["reply_id"] == r.id and reply_ev[0].data["intent"] == "interested"


def test_question_and_not_now_suggest_leads_too(db, installed) -> None:
    from sales0.db.models import Company
    from sales0.services.crm import lifecycle

    for intent in ("question", "not_now"):
        m, v, msg = _contacted(db)
        _reply(db, m, msg, intent=intent)
        assert db.scalar(select(Lead.stage).where(Lead.prospect_id == v.id)) is None, intent
        assert lifecycle.active_suggestion(db.get(Company, prospects.get_prospect(db, v.id).company_id)) is not None, intent


def test_reply_to_a_never_contacted_prospect_records_but_opens_no_lead(db, installed) -> None:
    m = h.member(db)
    v, e = _accepted(db)
    msg = h.sent_message(db, m, enquiry_row=e)  # sent outside the app: no MessageSent was published
    _reply(db, m, msg)
    p = prospects.get_prospect(db, v.id)
    assert p.responded_at is not None and db.scalar(select(func.count(Lead.id))) == 0 and p.status == "open"


def test_unsubscribe_sets_no_follow_up_cancels_reminders_and_opens_no_lead(db, installed) -> None:
    m, v, msg = _contacted(db)
    r = _reply(db, m, msg, intent="unsubscribe")
    p = prospects.get_prospect(db, v.id)
    assert [f.kind for f in p.flags] == ["no_follow_up"] and p.flags[0].detail == {"reason": "unsubscribe", "reply_id": r.id}
    assert p.flags[0].source == "auto" and db.scalar(select(func.count(Lead.id))) == 0
    assert db.scalar(select(func.count(FollowUp.id)).where(FollowUp.done_at.is_(None), FollowUp.cancelled_at.is_(None))) == 0
    assert followups.due_count(db, today=h.day(30)) == 0


def test_other_intent_records_the_response_only(db, installed) -> None:
    m, v, msg = _contacted(db)
    _reply(db, m, msg, intent="other")
    assert prospects.get_prospect(db, v.id).responded_at is not None and db.scalar(select(func.count(Lead.id))) == 0


def test_auto_reply_and_bounce_never_count(db, installed) -> None:
    m, v, msg = _contacted(db)
    _reply(db, m, msg, kind="auto_reply", intent=None)
    _reply(db, m, msg, kind="bounce", intent=None)
    p = prospects.get_prospect(db, v.id)
    assert p.responded_at is None and p.status == "open" and db.scalar(select(func.count(Lead.id))) == 0
    notes = [t for t in p.timeline if t.kind == "note"]
    assert len(notes) == 2 and p.stage == "contacted"
    assert db.scalar(select(func.count(FollowUp.id)).where(FollowUp.cancelled_at.is_(None))) == 3  # reminders intact


def test_a_second_interested_reply_opens_no_lead_and_redelivery_is_idempotent(db, installed) -> None:
    m, v, msg = _contacted(db)
    r = _reply(db, m, msg)
    _reply(db, m, msg)
    events.publish(db, events.ReplyReceived(outreach_message_id=msg.id, kind="human", intent="interested", reply_id=r.id))
    assert db.scalar(select(func.count(Lead.id))) == 0
    assert [t.kind for t in prospects.get_prospect(db, v.id).timeline].count("reply_received") == 2


def test_reply_after_a_lost_lead_suggests_again_but_opens_nothing(db, installed) -> None:
    from sales0.db.models import Company
    from sales0.services.crm import leads, lifecycle

    m, v, msg = _contacted(db)
    p = prospects.get_prospect(db, v.id)
    lifecycle.mark_lead(db, p.company_id, now=h.T0)  # a person decided
    lid = db.scalar(select(Lead.id).where(Lead.prospect_id == v.id))
    leads.move_stage(db, lid, "lost", now=h.T0)
    _reply(db, m, msg, intent="interested")
    assert db.scalar(select(func.count(Lead.id)).where(Lead.prospect_id == v.id)) == 1
    assert db.get(Company, p.company_id).off_ramp == "not_interested"


def test_reply_to_a_message_without_a_prospect_is_ignored(db, installed) -> None:
    m = h.member(db)
    msg = h.sent_message(db, m, to="who@nowhere.example")
    events.publish(db, events.ReplyReceived(outreach_message_id=msg.id, kind="human", intent="interested", reply_id=None))
    assert db.scalar(select(func.count(Prospect.id))) == 0


# ---------------------------------------------------------------- isolation
def test_a_failing_hook_never_breaks_the_publisher(db, installed, monkeypatch) -> None:
    v, e = _accepted(db)
    m = h.member(db)
    msg = h.sent_message(db, m, enquiry_row=e)

    def boom(*a, **k):
        raise RuntimeError("crm exploded")

    monkeypatch.setattr(hooks.prospects, "mark_contacted", boom)
    n = events.publish(db, events.MessageSent(outreach_message_id=msg.id, member_id=m.id, to_addrs=("asha@acme.example",), sent_at=h.T0))
    assert n == 1  # the guarded handler swallowed the error (publish counts it as ok)
    assert prospects.get_prospect(db, v.id).stage == "new"
    assert db.get(OutreachMessage, msg.id) is not None and db.get(Enquiry, e.id) is not None
    assert not flags.has_active(db, "prospect", v.id, "no_follow_up")
