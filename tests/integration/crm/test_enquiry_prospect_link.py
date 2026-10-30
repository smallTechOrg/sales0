"""Enquiry status <-> prospect status (deals.py module doc). They are different things; the ONE link: contacted /
follow-up N on ANY accepted enquiry of a company that is still a new prospect moves the prospect forward to the same
stage (forward only, no error when it cannot). The prospect's own forward moves carry its open enquiries along (parked statuses stay)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from sales0.db.models import Company, Enquiry, Prospect, TimelineEvent
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


def test_company_cadence_moves_open_enquiries_but_not_parked_ones(db) -> None:
    e1, v, co = _accepted(db)
    e2 = _second(db)
    deals.set_status(db, e2.id, "on_hold", now=h.T0)
    prospects.mark_contacted(db, v.id, when=h.T0, now=h.T0)
    prospects.move_stage(db, v.id, "follow_up_2")
    db.refresh(co), db.refresh(e1), db.refresh(e2)
    assert co.sub_status == "follow_up_2"
    assert e1.deal_status == "follow_up_2" and e2.deal_status == "on_hold"  # parked statuses stay


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


def test_enquiries_without_a_company_each_get_their_own_placeholder(db) -> None:
    a = h.enquiry(db, profile_row=None, company=None)
    b = h.enquiry(db, profile_row=None, company=None)
    prospects.ensure_for_enquiry(db, a.id)
    prospects.ensure_for_enquiry(db, b.id)
    db.flush()
    ca, cb = db.get(Enquiry, a.id).company_id, db.get(Enquiry, b.id).company_id
    assert ca and cb and ca != cb  # never one shared "Unnamed" bucket
    assert db.get(Company, ca).name.startswith(f"Company TBD #{a.id}")
    assert "Unnamed" not in db.get(Company, cb).name


def test_attaching_a_company_replaces_the_placeholder(db) -> None:
    from sales0.services.crm import companies

    e = h.enquiry(db, profile_row=None, company=None)
    prospects.ensure_for_enquiry(db, e.id)
    db.flush()
    old_id = db.get(Enquiry, e.id).company_id
    companies.attach_to_enquiry(db, e.id, name="Real Co Pvt Ltd")
    db.flush()
    row = db.get(Enquiry, e.id)
    new = db.get(Company, row.company_id)
    assert new.name == "Real Co Pvt Ltd" and row.company == "Real Co Pvt Ltd" and row.company_id != old_id
    assert db.get(Company, old_id) is None  # the emptied placeholder was folded away
    assert db.get(Prospect, row.prospect_id).company_id == new.id
    # attaching an existing company by name joins it instead of duplicating
    f = h.enquiry(db, profile_row=None, company=None)
    prospects.ensure_for_enquiry(db, f.id)
    companies.attach_to_enquiry(db, f.id, name="real co pvt ltd")
    db.flush()
    assert db.get(Enquiry, f.id).company_id == new.id


def test_attach_requires_a_name(db) -> None:
    from sales0.services.crm import companies

    e = h.enquiry(db, profile_row=None, company=None)
    with pytest.raises(DomainError):
        companies.attach_to_enquiry(db, e.id, name="  ")


def test_contacting_the_prospect_moves_its_enquiries_forward(db) -> None:
    e1, v, _ = _accepted(db)
    e2 = _second(db)
    prospects.log_contact(db, v.id, channel="email", actor=Actor("Ana", None), now=h.T0)
    db.refresh(e1), db.refresh(e2)
    assert (e1.deal_status, e2.deal_status) == ("contacted", "contacted")


def test_prospect_stage_never_moves_an_enquiry_backwards_or_out_of_a_parked_status(db) -> None:
    e1, v, _ = _accepted(db)
    e2 = _second(db)
    deals.set_status(db, e1.id, "on_hold", now=h.T0)
    deals.set_status(db, e2.id, "follow_up_2", now=h.T0)
    prospects.move_stage(db, v.id, "contacted", now=h.T0)
    db.refresh(e1), db.refresh(e2)
    assert (e1.deal_status, e2.deal_status) == ("on_hold", "follow_up_2")


def test_relinking_heals_an_empty_derived_priority(db) -> None:
    e, v, _ = _accepted(db)
    e.priority = "high"
    db.flush()
    p = db.get(Prospect, v.id)
    assert p.priority is None
    prospects.ensure_for_enquiry(db, e.id)
    db.refresh(p)
    assert p.priority == "high"
