"""The company model: stage / sub-status projection, Mark as lead, Won -> Client, off-ramps, suggest-Lead (never
auto-moves), Do not contact."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from sales0.db.models import Company, Enquiry, Prospect, TimelineEvent
from sales0.errors import DomainError
from sales0.services.crm import deals, lifecycle, prospects
from sales0.services.crm.actor import Actor
from tests.integration.crm import helpers as h


def _accepted(db, company: str = "Acme Training", **kw):
    e = h.enquiry(db, profile_row=h.profile(db, slug=f"in/p{h._ids.__next__()}"), company=company, **kw)
    v = prospects.ensure_for_enquiry(db, e.id)
    db.flush()
    co = db.get(Company, db.get(Enquiry, e.id).company_id)
    return e, v, co


def test_accept_creates_prospect_new_company_and_open_enquiry(db) -> None:
    e, _, co = _accepted(db)
    db.refresh(co)
    assert (co.stage, co.sub_status, co.off_ramp, co.stage_source) == ("prospect", "new", None, "app")
    db.refresh(e)
    assert (e.deal_status, e.deal_status_source) == ("open", None)


def test_enquiry_without_company_name_still_gets_a_company(db) -> None:
    e = h.enquiry(db, company=None, author_name="Solo Founder")
    prospects.ensure_for_enquiry(db, e.id)
    db.refresh(e)
    co = db.get(Company, e.company_id)
    assert co is not None and co.name == "Solo Founder" and co.stage == "prospect"


def test_second_enquiry_attaches_and_never_demotes_the_stage(db) -> None:
    e1, v, co = _accepted(db)
    lifecycle.mark_client(db, co.id, actor=Actor("Ana", None), now=h.T0)
    db.refresh(co)
    assert (co.stage, co.sub_status) == ("client", "active")
    e2 = h.enquiry(db, profile_row=h.profile(db, slug="in/another"), company="Acme Training")
    v2 = prospects.ensure_for_enquiry(db, e2.id)
    db.refresh(co)
    db.refresh(e2)
    assert v2.id == v.id  # one outreach record per company
    assert e2.company_id == co.id and co.stage == "client"  # upsell: the company stays a client


def test_enquiry_status_survives_the_cadence(db) -> None:
    e, v, co = _accepted(db)
    deals.set_status(db, e.id, "on_hold")
    prospects.mark_contacted(db, v.id, when=h.T0, now=h.T0)
    db.refresh(e)
    assert e.deal_status == "on_hold" and e.deal_status_source == "app"


def test_meeting_or_proposal_only_suggests_lead(db) -> None:
    e, _, co = _accepted(db)
    deals.set_status(db, e.id, "meeting_scheduled")
    db.refresh(co)
    assert co.stage == "prospect"  # the app never moves it
    assert lifecycle.active_suggestion(co) is not None
    lifecycle.dismiss_suggestion(db, co.id)
    db.refresh(co)
    assert lifecycle.active_suggestion(co) is None
    deals.set_status(db, e.id, "proposal_sent")  # a different signal raises it again
    db.refresh(co)
    assert lifecycle.active_suggestion(co) is not None
    lifecycle.accept_suggestion(db, co.id)
    db.refresh(co)
    assert (co.stage, co.sub_status, co.lead_suggestion) == ("lead", "meeting_scheduled", None)


def test_mark_lead_overrides_the_preconditions(db) -> None:
    _, _, co = _accepted(db)  # never contacted, no response: the old Convert refused
    lifecycle.mark_lead(db, co.id, stage="proposal_sent", actor=Actor("Ana", None))
    db.refresh(co)
    assert (co.stage, co.sub_status) == ("lead", "proposal_sent")
    lifecycle.set_lead_status(db, co.id, "meeting_scheduled")
    db.refresh(co)
    assert co.sub_status == "meeting_scheduled"


def test_won_enquiry_makes_the_company_a_client(db) -> None:
    e, _, co = _accepted(db)
    deals.set_status(db, e.id, "won", actor=Actor("Ana", None))
    db.refresh(co)
    assert (co.stage, co.sub_status) == ("client", "active")
    events = [t.body for t in db.scalars(select(TimelineEvent).where(TimelineEvent.subject_type == "company", TimelineEvent.subject_id == co.id))]
    assert any("Client" in (b or "") for b in events)


def test_mark_client_directly_for_a_company_without_enquiry(db) -> None:
    from sales0.services.crm import companies

    co = companies.get_or_create_row(db, "Typed In Ltd")
    assert co.stage is None  # a known company, not in the pipeline
    lifecycle.mark_client(db, co.id)
    db.refresh(co)
    assert co.stage == "client"


def test_off_ramps_and_reopen_restore_the_stage(db) -> None:
    _, v, co = _accepted(db)
    prospects.move_stage(db, v.id, "follow_up_2")
    lifecycle.go_off_ramp(db, co.id, "dormant", actor=Actor("Ana", None))
    db.refresh(co)
    assert (co.off_ramp, co.stage, co.sub_status, co.resume_sub_status) == ("dormant", "prospect", "follow_up_2", "follow_up_2")
    lifecycle.reopen(db, co.id)
    db.refresh(co)
    assert (co.off_ramp, co.stage, co.sub_status) == (None, "prospect", "follow_up_2")
    lifecycle.mark_lead(db, co.id)
    lifecycle.go_off_ramp(db, co.id, "not_interested", reason="budget")
    db.refresh(co)
    assert (co.off_ramp, co.stage) == ("not_interested", "lead")
    lifecycle.reopen(db, co.id)
    db.refresh(co)
    assert (co.off_ramp, co.stage) == (None, "lead")


def test_do_not_contact_blocks_outreach_and_needs_an_admin_to_reopen(db) -> None:
    _, v, co = _accepted(db)
    prospects.mark_contacted(db, v.id, when=h.T0, now=h.T0)
    lifecycle.go_off_ramp(db, co.id, "do_not_contact", reason="asked to stop")
    db.refresh(co)
    assert co.off_ramp == "do_not_contact" and co.stage == "prospect"
    with pytest.raises(DomainError) as ei:
        lifecycle.ensure_contactable(db, company_id=co.id)
    assert ei.value.code == "do_not_contact"
    with pytest.raises(DomainError) as ei2:
        lifecycle.ensure_contactable(db, prospect_id=v.id)
    assert ei2.value.code == "do_not_contact"
    # the ledger changing (e.g. the sheet) never clears a company-only off-ramp
    prospects.move_stage(db, v.id, "follow_up_1")
    db.refresh(co)
    assert co.off_ramp == "do_not_contact" and co.sub_status == "follow_up_1"
    with pytest.raises(DomainError) as ei3:
        lifecycle.reopen(db, co.id, is_admin=False)
    assert ei3.value.http_status == 403
    lifecycle.reopen(db, co.id, is_admin=True)
    db.refresh(co)
    assert co.off_ramp is None
    lifecycle.ensure_contactable(db, company_id=co.id)


def test_lost_client_only_for_clients_and_reopen(db) -> None:
    _, _, co = _accepted(db)
    with pytest.raises(DomainError):
        lifecycle.go_off_ramp(db, co.id, "lost_client")
    lifecycle.mark_client(db, co.id)
    lifecycle.go_off_ramp(db, co.id, "lost_client", reason="churned")
    db.refresh(co)
    assert (co.stage, co.off_ramp) == ("client", "lost_client")
    lifecycle.reopen(db, co.id)
    db.refresh(co)
    assert (co.stage, co.off_ramp) == ("client", None)


def test_reply_only_suggests_a_lead(db, installed) -> None:
    from sales0.services import events

    e, v, co = _accepted(db)
    m = h.member(db)
    msg = h.sent_message(db, m, enquiry_row=e)
    prospects.mark_contacted(db, v.id, when=h.T0, now=h.T0)
    r = h.reply(db, msg, intent="interested")
    events.publish(db, events.ReplyReceived(outreach_message_id=msg.id, kind="human", intent="interested", reply_id=r.id, member_id=m.id, from_addr=r.from_addr))
    db.refresh(co)
    assert co.stage == "prospect"  # not moved
    assert lifecycle.active_suggestion(co) is not None
    assert db.scalar(select(Prospect.status).where(Prospect.id == v.id)) == "open"
