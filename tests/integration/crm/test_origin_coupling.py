"""Origin-enquiry <-> prospect-stage coupling (deals.py module doc): the enquiry that brought a company in drives the
company's prospect sub-status when a person sets contacted / follow-up N on it; non-origin enquiries never do; only
while the company is an active prospect; the company's own cadence still mirrors onto driven enquiries without loops."""

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


def test_origin_is_the_earliest_accepted_enquiry(db) -> None:
    e1, _, co = _accepted(db)
    e2 = _second(db)
    assert deals.origin_enquiry_id(db, co.id) == e1.id
    assert deals.is_origin(db, e1) and not deals.is_origin(db, e2)
    assert "contacted" in deals.settable_for(db, e1) and "contacted" not in deals.settable_for(db, e2)


def test_origin_status_moves_the_company_and_mirrors_to_open_enquiries(db) -> None:
    e1, _, co = _accepted(db)
    e2 = _second(db)
    deals.set_status(db, e1.id, "follow_up_1", actor=Actor("Ana", None), now=h.T0)
    db.refresh(co), db.refresh(e1), db.refresh(e2)
    assert (co.stage, co.sub_status) == ("prospect", "follow_up_1")
    assert e1.deal_status == "follow_up_1" and e2.deal_status == "follow_up_1"  # the company's cadence mirrors (as before)
    bodies = [t.body or "" for t in db.scalars(select(TimelineEvent).where(TimelineEvent.company_id == co.id))]
    assert any("Company: Prospect / new -> Prospect / follow up 1" in b for b in bodies)
    assert any("prospect stage follows" in b for b in bodies)


def test_by_hand_can_move_back_but_other_sources_never_move_backwards(db) -> None:
    e1, _, co = _accepted(db)
    deals.set_status(db, e1.id, "follow_up_2", now=h.T0)
    deals.set_status(db, e1.id, "follow_up_1", now=h.T0)  # a person correcting it on the enquiry
    db.refresh(co)
    assert co.sub_status == "follow_up_1"
    deals.set_status(db, e1.id, "follow_up_2", now=h.T0)
    deals.set_status(db, e1.id, "contacted", source="sheet", now=h.T0)  # not by hand: forward only
    db.refresh(co), db.refresh(e1)
    assert co.sub_status == "follow_up_2" and e1.deal_status == "follow_up_2"
    deals.set_status(db, e1.id, "final_follow_up", source="sheet", now=h.T0)
    db.refresh(co)
    assert co.sub_status == "final_follow_up"


def test_non_origin_enquiry_does_not_drive_the_stage(db) -> None:
    _, _, co = _accepted(db)
    e2 = _second(db)
    with pytest.raises(DomainError) as ei:
        deals.set_status(db, e2.id, "contacted")
    assert ei.value.code == "status_driven"
    db.refresh(co)
    assert co.sub_status == "new"


def test_only_while_the_company_is_an_active_prospect(db) -> None:
    e1, _, co = _accepted(db)
    lifecycle.mark_lead(db, co.id, actor=Actor("Ana", None), now=h.T0)
    with pytest.raises(DomainError) as ei:
        deals.set_status(db, e1.id, "contacted")
    assert ei.value.code == "stage_invalid"
    db.refresh(co)
    assert co.stage == "lead"
    e3, _, co3 = _accepted(db, "Closed Co")
    lifecycle.go_off_ramp(db, co3.id, "not_interested", actor=Actor("Ana", None), now=h.T0)
    with pytest.raises(DomainError):
        deals.set_status(db, e3.id, "contacted")


def test_meeting_or_proposal_on_the_origin_still_only_suggests_lead(db) -> None:
    e1, _, co = _accepted(db)
    deals.set_status(db, e1.id, "contacted", now=h.T0)
    deals.set_status(db, e1.id, "meeting_scheduled", now=h.T0)
    db.refresh(co)
    assert co.stage == "prospect" and lifecycle.active_suggestion(co) is not None  # a person confirms


def test_no_ping_pong_company_to_enquiry_and_back(db) -> None:
    e1, v, co = _accepted(db)
    deals.set_status(db, e1.id, "contacted", now=h.T0)
    before = _events(db, co, "company")
    deals.set_status(db, e1.id, "contacted", now=h.T0)  # same value again: nothing changes, nothing is written
    assert _events(db, co, "company") == before
    prospects.move_stage(db, v.id, "follow_up_2")  # company-side cadence change mirrors onto the open enquiry...
    db.refresh(co), db.refresh(e1)
    assert (co.sub_status, e1.deal_status) == ("follow_up_2", "follow_up_2")
    enquiry_events = _events(db, co, "enquiry")
    db.flush()
    db.refresh(co)
    assert co.sub_status == "follow_up_2"  # ...and does not come back as another company move
    assert _events(db, co, "enquiry") == enquiry_events


def test_leaving_a_hand_status_for_a_cadence_status_on_the_origin(db) -> None:
    e1, _, co = _accepted(db)
    deals.set_status(db, e1.id, "on_hold", now=h.T0)
    deals.set_status(db, e1.id, "contacted", now=h.T0)
    db.refresh(co), db.refresh(e1)
    assert (co.sub_status, e1.deal_status, e1.deal_status_source) == ("contacted", "contacted", "driven")
