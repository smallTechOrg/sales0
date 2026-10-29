"""Enquiry status <-> prospect status (deals.py module doc). They are different things; the ONE link: contacted /
follow-up N on ANY accepted enquiry of a company that is still a new prospect moves the prospect forward to the same
stage (forward only, no error when it cannot). The company's own cadence never rewrites enquiry statuses."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from sales0.db.models import Company, Enquiry, TimelineEvent
from sales0.errors import DomainError
from sales0.services.crm import deals, lifecycle, prospects
from sales0.services.crm.actor import Actor
from tests.integration.crm import helpers as h


def _accepted(db, company: str = "Acme Training"):
    e = h.enquiry(db, profile_row=h.profile(db, slug=f"in/o{h._ids.__next__()}"), company=company)
    v = prospects.ensure_for_enquiry(db, e.id)
    db.flush()
    return e, v, db.get(Company, db.get(Enquiry, e.id).company_id)


def _second(db, company: str = "Acme Training"):
    e = h.enquiry(db, profile_row=h.profile(db, slug=f"in/o{h._ids.__next__()}"), company=company)
    prospects.ensure_for_enquiry(db, e.id)
    db.flush()
    return e


def _events(db, co, subject_type: str) -> int:
    return int(db.scalar(select(func.count()).select_from(TimelineEvent).where(
        TimelineEvent.company_id == co.id, TimelineEvent.subject_type == subject_type)) or 0)


def test_every_enquiry_gets_the_same_statuses(db) -> None:
    assert deals.SETTABLE == ("open", "contacted", "follow_up_1", "follow_up_2", "final_follow_up", "meeting_scheduled",
                              "proposal_sent", "won", "lost", "no_response", "on_hold")
    assert [deals.deal_label(k) for k in deals.SETTABLE][:5] == ["Open", "Contacted", "Follow-up 1", "Follow-up 2", "Final follow-up"]


@pytest.mark.parametrize("which", ["first", "second"])
def test_any_enquiry_of_a_new_prospect_moves_it_forward(db, which) -> None:
    e1, _, co = _accepted(db)
    e = e1 if which == "first" else _second(db)
    deals.set_status(db, e.id, "follow_up_1", actor=Actor("Ana", None), now=h.T0)
    db.refresh(co), db.refresh(e)
    assert (co.stage, co.sub_status) == ("prospect", "follow_up_1") and e.deal_status == "follow_up_1"
    bodies = [t.body or "" for t in db.scalars(select(TimelineEvent).where(TimelineEvent.company_id == co.id))]
    assert any("Company: Prospect / new -> Prospect / follow up 1" in b for b in bodies)
    assert any("prospect stage follows" in b for b in bodies)


def test_the_prospect_never_moves_backwards_and_no_error(db) -> None:
    e1, _, co = _accepted(db)
    e2 = _second(db)
    deals.set_status(db, e1.id, "follow_up_2", now=h.T0)
    deals.set_status(db, e2.id, "contacted", now=h.T0)  # behind the prospect: only the enquiry changes
    db.refresh(co), db.refresh(e2)
    assert co.sub_status == "follow_up_2" and e2.deal_status == "contacted"
    deals.set_status(db, e1.id, "follow_up_1", now=h.T0)  # same on the enquiry that moved it
    db.refresh(co), db.refresh(e1)
    assert co.sub_status == "follow_up_2" and e1.deal_status == "follow_up_1"
    deals.set_status(db, e2.id, "final_follow_up", now=h.T0)
    db.refresh(co)
    assert co.sub_status == "final_follow_up"


def test_open_lost_no_response_on_hold_leave_the_prospect_alone(db) -> None:
    e1, _, co = _accepted(db)
    deals.set_status(db, e1.id, "contacted", now=h.T0)
    for st in ("lost", "no_response", "on_hold", "open"):
        deals.set_status(db, e1.id, st, now=h.T0)
        db.refresh(co), db.refresh(e1)
        assert (co.stage, co.sub_status, e1.deal_status) == ("prospect", "contacted", st)


def test_lead_client_and_off_ramp_companies_are_unaffected_without_error(db) -> None:
    e1, _, co = _accepted(db)
    lifecycle.mark_lead(db, co.id, actor=Actor("Ana", None), now=h.T0)
    deals.set_status(db, e1.id, "contacted", now=h.T0)
    db.refresh(co), db.refresh(e1)
    assert co.stage == "lead" and e1.deal_status == "contacted"
    e3, _, co3 = _accepted(db, "Closed Co")
    lifecycle.go_off_ramp(db, co3.id, "not_interested", actor=Actor("Ana", None), now=h.T0)
    db.refresh(co3)
    sub = co3.sub_status
    deals.set_status(db, e3.id, "follow_up_1", now=h.T0)
    db.refresh(co3), db.refresh(e3)
    assert co3.off_ramp == "not_interested" and co3.sub_status == sub and e3.deal_status == "follow_up_1"
    e4, _, co4 = _accepted(db, "Client Co")
    lifecycle.mark_client(db, co4.id, actor=Actor("Ana", None), now=h.T0)
    deals.set_status(db, e4.id, "contacted", now=h.T0)
    db.refresh(co4)
    assert co4.stage == "client"


def test_a_sheet_never_moves_a_stage_set_in_the_app(db) -> None:
    e1, _, co = _accepted(db)
    assert co.stage_source == "app"
    deals.set_status(db, e1.id, "contacted", source="sheet", now=h.T0)
    db.refresh(co)
    assert co.sub_status == "new"


def test_meeting_or_proposal_still_only_suggests_lead(db) -> None:
    e1, _, co = _accepted(db)
    deals.set_status(db, e1.id, "meeting_scheduled", now=h.T0)
    db.refresh(co)
    assert co.stage == "prospect" and lifecycle.active_suggestion(co) is not None  # a person confirms
    deals.set_status(db, e1.id, "won", now=h.T0)
    db.refresh(co)
    assert co.stage == "client"


def test_company_cadence_does_not_touch_enquiry_statuses(db) -> None:
    e1, v, co = _accepted(db)
    e2 = _second(db)
    deals.set_status(db, e2.id, "on_hold", now=h.T0)
    prospects.mark_contacted(db, v.id, when=h.T0, now=h.T0)
    prospects.move_stage(db, v.id, "follow_up_2")
    db.refresh(co), db.refresh(e1), db.refresh(e2)
    assert co.sub_status == "follow_up_2"
    assert e1.deal_status == "open" and e2.deal_status == "on_hold"  # nothing written back to the enquiries


def test_no_ping_pong(db) -> None:
    e1, _, co = _accepted(db)
    deals.set_status(db, e1.id, "contacted", now=h.T0)
    before = _events(db, co, "company")
    deals.set_status(db, e1.id, "contacted", now=h.T0)  # same value again: nothing changes, nothing is written
    assert _events(db, co, "company") == before
    db.flush()
    db.refresh(co)
    assert co.sub_status == "contacted"


def test_invalid_status_is_refused(db) -> None:
    e1, _, _ = _accepted(db)
    with pytest.raises(DomainError):
        deals.set_status(db, e1.id, "nonsense")
