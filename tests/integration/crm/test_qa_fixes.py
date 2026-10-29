"""Regression tests for the QA findings B1, M1, M2, M3, L2 (hooks, merge, tracking, timeline coverage)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from sales0.db.models import Contact, FollowUp, Lead, LinkedinProfile, Prospect, TimelineEvent
from sales0.errors import DomainError
from sales0.services import events
from sales0.services.crm import companies, contacts, followups, leads, prospects
from tests.integration.crm import helpers as h

REPLY_AT = h.T0 + timedelta(days=1)


def _accepted(db, slug="in/qa1", email="asha@acme.example"):
    prof = h.profile(db, slug=slug)
    e = h.enquiry(db, profile_row=prof, company="Acme Training", email=email)
    return prospects.ensure_for_enquiry(db, e.id), e, prof


def _send(db, m, e, to="asha@acme.example", at=None):
    msg = h.sent_message(db, m, enquiry_row=e, to=to)
    events.publish(db, events.MessageSent(outreach_message_id=msg.id, member_id=m.id, to_addrs=(to,), sent_at=at or h.T0))
    return msg


def _reply(db, m, msg, intent="interested", at=REPLY_AT):
    r = h.reply(db, msg, kind="human", intent=intent, at=at)
    events.publish(db, events.ReplyReceived(outreach_message_id=msg.id, kind="human", intent=intent, reply_id=r.id, member_id=m.id))
    return r


def _kinds(db, pid):
    return [t.kind for t in prospects.get_prospect(db, pid).timeline]


# ------------------------------------------------------------------------------------------------ B1
def test_b1_email_to_closed_prospect_then_interested_reply_only_suggests(db, installed) -> None:
    m = h.member(db)
    v, e, prof = _accepted(db)
    prospects.close(db, v.id, "not_interested")
    assert db.get(LinkedinProfile, prof.id).tracked is False
    msg = _send(db, m, e)
    p = db.get(Prospect, v.id)
    assert p.contacted_at is not None and p.status == "closed" and "email_sent" in _kinds(db, v.id)
    _reply(db, m, msg)
    db.expire_all()
    p = db.get(Prospect, v.id)
    assert p.status == "closed" and p.responded_at is not None  # a closed company is never reopened by the app
    assert db.scalar(select(Lead).where(Lead.prospect_id == v.id)) is None
    from sales0.db.models import Company

    assert db.get(Company, p.company_id).lead_suggestion["kind"] == "reply"


def test_b1_closed_prospect_reply_without_recorded_contact_is_explained_not_silent(db, installed) -> None:
    m = h.member(db)
    v, e, _ = _accepted(db)
    msg = h.sent_message(db, m, enquiry_row=e, state="approved", sent_at=None)  # never sent
    prospects.close(db, v.id, "duplicate")
    _reply(db, m, msg, intent="question")
    notes = [t.body for t in prospects.get_prospect(db, v.id).timeline if t.kind == "note"]
    assert any("no lead suggested" in (b or "") for b in notes)
    assert db.scalar(select(Lead.id).where(Lead.prospect_id == v.id)) is None


def test_b1_not_now_reply_after_close_stays_closed(db, installed) -> None:
    m = h.member(db)
    v, e, _ = _accepted(db)
    msg = _send(db, m, e)
    prospects.close(db, v.id, "no_response")
    _reply(db, m, msg, intent="not_now")
    assert db.get(Prospect, v.id).status == "closed"


# ------------------------------------------------------------------------------------------------ M1
def test_m1_first_email_after_manual_contact_does_not_advance_the_stage(db, installed) -> None:
    m = h.member(db)
    v, e, _ = _accepted(db)
    prospects.mark_contacted(db, v.id, channel="linkedin", when=h.T0 - timedelta(days=2), now=h.T0 - timedelta(days=2))
    reminders = [f.kind for f in db.scalars(select(FollowUp).where(FollowUp.prospect_id == v.id))]
    assert reminders == ["follow_up_1", "follow_up_2", "final_follow_up"]
    msg = _send(db, m, e)
    p = db.get(Prospect, v.id)
    assert p.stage == "contacted"
    assert "email_sent" in _kinds(db, v.id)
    assert not [f for f in db.scalars(select(FollowUp).where(FollowUp.prospect_id == v.id)) if f.done_at]
    # the SECOND email is a real follow-up
    _send(db, m, e, at=h.T0 + timedelta(days=5))
    assert db.get(Prospect, v.id).stage == "follow_up_1"
    assert msg.id


def test_m1_first_email_after_manual_contact_schedules_reminders_when_none_exist(db, installed) -> None:
    m = h.member(db)
    v, e, _ = _accepted(db)
    prospects.mark_contacted(db, v.id, channel="call", when=h.T0, now=h.T0)
    for f in db.scalars(select(FollowUp).where(FollowUp.prospect_id == v.id)):
        db.delete(f)
    db.flush()
    _send(db, m, e)
    assert db.get(Prospect, v.id).stage == "contacted"
    assert len(list(db.scalars(select(FollowUp).where(FollowUp.prospect_id == v.id, FollowUp.auto.is_(True))))) == 3


# ------------------------------------------------------------------------------------------------ M2
def test_m2_merge_keeps_the_dropped_linkedin_profile_and_resolves_the_old_id(db) -> None:
    a = contacts.upsert_row(db, full_name="Asha Rao", email="a@a.example", linkedin_url="https://www.linkedin.com/in/asha-a/")[0]
    b = contacts.upsert_row(db, full_name="Asha R", email="b@b.example", linkedin_url="https://www.linkedin.com/in/asha-b/")[0]
    keep_id, drop_id = a.id, b.id
    contacts.merge(db, keep_id, drop_id)
    detail = contacts.get_contact(db, keep_id)
    assert "linkedin.com/in/asha-b" in (detail.notes or "")
    ev = next(t for t in detail.timeline if t.kind == "contact_merged")
    assert ev.data["dropped_slugs"] == ["in/asha-b"] and ev.data["dropped_id"] == drop_id
    # the stale id resolves to the survivor, in reads and in merges
    assert contacts.resolve_id(db, drop_id) == keep_id
    assert contacts.get_contact(db, drop_id).id == keep_id
    # a later import carrying the dropped profile lands on the survivor, not on a new duplicate
    again, created = contacts.upsert_row(db, full_name="Asha R", linkedin_url="https://www.linkedin.com/in/asha-b/")
    assert not created and again.id == keep_id
    with pytest.raises(DomainError):
        contacts.merge(db, keep_id, drop_id)  # resolves to the same contact -> merge with itself


def test_m2_slug_moves_when_keep_has_none_and_chain_resolves(db) -> None:
    a = contacts.upsert_row(db, full_name="One", email="o@x.example")[0]
    b = contacts.upsert_row(db, full_name="Two", email="t@x.example", linkedin_url="https://www.linkedin.com/in/two/")[0]
    c = contacts.upsert_row(db, full_name="Three", email="th@x.example")[0]
    a_id, b_id, c_id = a.id, b.id, c.id
    contacts.merge(db, b_id, c_id)
    contacts.merge(db, a_id, b_id)
    assert db.get(Contact, a_id).linkedin_slug == "in/two"
    assert contacts.resolve_id(db, c_id) == a_id and contacts.resolve_id(db, b_id) == a_id
    assert contacts.resolve_id(db, 99999) is None


# ------------------------------------------------------------------------------------------------ M3
def test_m3_reply_after_lost_reopens_a_lead_and_restores_tracking(db, installed) -> None:
    m = h.member(db)
    v, e, prof = _accepted(db)
    msg = _send(db, m, e)
    _reply(db, m, msg)
    from sales0.services.crm import lifecycle

    lifecycle.mark_lead(db, db.get(Prospect, v.id).company_id)  # a person decides
    first = db.scalar(select(Lead))
    leads.move_stage(db, first.id, "lost")
    assert db.get(LinkedinProfile, prof.id).tracked is False
    lifecycle.mark_lead(db, db.get(Prospect, v.id).company_id)  # and again, after the loss
    db.expire_all()
    assert len(list(db.scalars(select(Lead).where(Lead.prospect_id == v.id)))) == 2
    assert db.get(LinkedinProfile, prof.id).tracked is True


def test_m3_moving_a_lead_back_from_lost_or_no_response_restores_tracking(db, installed) -> None:
    m = h.member(db)
    v, e, prof = _accepted(db)
    _reply(db, m, _send(db, m, e))
    from sales0.services.crm import lifecycle

    lifecycle.mark_lead(db, db.get(Prospect, v.id).company_id)
    lead = db.scalar(select(Lead))
    for terminal in ("lost", "no_response"):
        leads.move_stage(db, lead.id, terminal)
        assert db.get(LinkedinProfile, prof.id).tracked is False
        leads.move_stage(db, lead.id, "proposal_sent")
        assert db.get(LinkedinProfile, prof.id).tracked is True


# ------------------------------------------------------------------------------------------------ L2
def test_l2_complete_up_to_leaves_timeline_events(db) -> None:
    v = prospects.create_manual(db, name="Solo")
    prospects.mark_contacted(db, v.id, when=h.T0, now=h.T0)
    prospects.move_stage(db, v.id, "follow_up_2", now=h.T0)
    done = [t for t in prospects.get_prospect(db, v.id).timeline if t.kind == "follow_up_done"]
    assert len(done) == 2 and all(t.data.get("auto") for t in done)
    assert followups.complete_up_to(db, v.id, "final_follow_up", actor=prospects.TEAM) == 1


def test_l2_upsert_enrichment_is_logged_and_noop_upserts_are_not(db) -> None:
    row, created = contacts.upsert_row(db, full_name="Dev", email="dev@x.example")
    assert created
    contacts.upsert_row(db, email="dev@x.example")
    row, created = contacts.upsert_row(db, email="dev@x.example", phone="+91 98765 43210", title="CTO")
    assert not created and row.title == "CTO"
    contacts.upsert_row(db, full_name="Dev", email="dev@x.example; dev2@x.example")
    notes = [t for t in contacts.get_contact(db, row.id).timeline if t.kind == "note"]
    assert len(notes) == 2
    assert "phone" in notes[0].body or "phone" in notes[1].body
    assert any("extra email" in t.body for t in notes)


def test_l2_company_creation_is_logged_once(db) -> None:
    c = companies.get_or_create_row(db, "Zeta Works")
    companies.get_or_create_row(db, "zeta works", website="zeta.example")
    kinds = [t.kind for t in companies.get_company(db, c.id).timeline]
    assert kinds.count("created") == 1
    assert db.scalar(select(TimelineEvent.id).where(TimelineEvent.subject_type == "company", TimelineEvent.subject_id == c.id)) is not None


# ------------------------------------------------------------------------------------------------ L3
def test_l3_recompute_all_clears_a_stale_flag_and_skips_prospects_without_recent_enquiries(db) -> None:
    from sales0.services.crm import flags

    v, _, _ = _accepted(db, slug="in/l3")
    quiet = prospects.create_manual(db, name="Quiet one")
    flags.set_flag(db, "prospect", v.id, "repeat_poster", source="auto")  # stale: only one enquiry
    assert flags.recompute_all(db, now=h.T0) == {"repeat_poster": 0, "competitor_mention": 0}
    assert not flags.has_active(db, "prospect", v.id, "repeat_poster")
    assert not flags.has_active(db, "prospect", quiet.id, "repeat_poster")
