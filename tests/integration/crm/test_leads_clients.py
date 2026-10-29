"""Leads (guards, stages, tracked, proposal date) and clients (from lead, legacy, update, list)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from sales0.db.models import Client, Enquiry, FollowUp, LinkedinProfile, Prospect
from sales0.errors import DomainError
from sales0.services.crm import clients, leads, prospects
from sales0.services.crm.actor import IMPORT, Actor
from tests.integration.crm import helpers as h


def _contacted_and_responded(db, n: int = 1):
    e = h.enquiry(db, profile_row=h.profile(db, slug=f"in/lead{n}{h._ids.__next__()}"), company="Acme Training")
    v = prospects.ensure_for_enquiry(db, e.id)
    prospects.mark_contacted(db, v.id, when=h.T0, now=h.T0)
    prospects.record_response(db, v.id, now=h.T0 + timedelta(days=1))
    return v, e


def test_convert_needs_contact_then_response(db) -> None:
    e = h.enquiry(db, company="Acme Training")
    v = prospects.ensure_for_enquiry(db, e.id)
    with pytest.raises(DomainError) as ei:
        leads.convert(db, v.id)
    assert ei.value.code == "lead_needs_contact" and ei.value.http_status == 409
    assert "mark it Contacted first" in ei.value.message
    prospects.mark_contacted(db, v.id, when=h.T0, now=h.T0)
    with pytest.raises(DomainError) as ei2:
        leads.convert(db, v.id)
    assert ei2.value.code == "lead_needs_response" and "Response received" in ei2.value.message


def test_convert_creates_one_open_lead_and_converts_the_prospect(db) -> None:
    v, _ = _contacted_and_responded(db)
    ld = leads.convert(db, v.id, note="warm", now=h.T0)
    assert ld.stage == "meeting_scheduled" and ld.prospect_name == "Acme Training" and ld.company_name == "Acme Training"
    row = db.get(Prospect, v.id)
    assert row.status == "converted"
    assert db.scalar(select(func.count(FollowUp.id)).where(FollowUp.prospect_id == v.id, FollowUp.cancelled_at.is_(None), FollowUp.done_at.is_(None))) == 0
    with pytest.raises(DomainError) as ei:
        leads.convert(db, v.id)
    assert ei.value.code == "lead_exists"
    ks = [t.kind for t in prospects.get_prospect(db, v.id).timeline]
    assert "lead_created" in ks
    assert prospects.get_prospect(db, v.id).lead.id == ld.id
    assert prospects.get_prospect(db, v.id).open_lead_id == ld.id


def test_convert_validates_stage_and_closed_prospect(db) -> None:
    v, _ = _contacted_and_responded(db)
    with pytest.raises(DomainError) as ei:
        leads.convert(db, v.id, stage="won")
    assert ei.value.code == "stage_invalid"
    prospects.close(db, v.id, "duplicate")
    with pytest.raises(DomainError) as ei2:
        leads.convert(db, v.id)
    assert ei2.value.code == "prospect_closed"


def test_full_lifecycle_proposal_won_creates_exactly_one_client(db) -> None:
    v, e = _contacted_and_responded(db)
    ld = leads.convert(db, v.id, now=h.T0)
    p = leads.move_stage(db, ld.id, "proposal_sent", proposal_sent_on=date(2026, 10, 12), value_estimate=250000)
    assert p.proposal_sent_on == date(2026, 10, 12) and p.value_estimate == 250000
    db.refresh(e)
    assert e.date_proposal_sent == date(2026, 10, 12)
    won = leads.move_stage(db, ld.id, "won", outcome_note="PO received", now=h.T0)
    assert won.closed_at is not None and won.outcome_note == "PO received"
    leads.move_stage(db, ld.id, "won", now=h.T0)  # idempotent
    assert db.scalar(select(func.count(Client.id))) == 1
    c = clients.list_clients(db).items[0]
    assert c.status == "active" and c.source == "lead" and c.lead_id == ld.id and c.prospect_id == v.id
    ks = [t.kind for t in prospects.get_prospect(db, v.id).timeline]
    assert {"lead_created", "proposal_sent", "lead_stage", "client_created"} <= set(ks)
    assert prospects.get_prospect(db, v.id).client.id == c.id


def test_proposal_sent_defaults_to_local_today(db) -> None:
    v, _ = _contacted_and_responded(db)
    ld = leads.convert(db, v.id, now=h.T0)
    out = leads.move_stage(db, ld.id, "proposal_sent", now=h.T0)
    assert out.proposal_sent_on == date(2026, 10, 1)


@pytest.mark.parametrize("stage", ["lost", "no_response"])
def test_lost_and_no_response_untrack_the_profile(db, stage) -> None:
    v, _ = _contacted_and_responded(db)
    ld = leads.convert(db, v.id, now=h.T0)
    out = leads.move_stage(db, ld.id, stage, now=h.T0)
    assert out.closed_at is not None
    assert db.get(LinkedinProfile, db.get(Prospect, v.id).linkedin_profile_id).tracked is False
    assert db.scalar(select(func.count(Client.id))) == 0


def test_moving_back_clears_closed_at_but_keeps_the_client(db) -> None:
    v, _ = _contacted_and_responded(db)
    ld = leads.convert(db, v.id, now=h.T0)
    leads.move_stage(db, ld.id, "won", now=h.T0)
    back = leads.move_stage(db, ld.id, "proposal_sent", now=h.T0)
    assert back.closed_at is None and back.stage == "proposal_sent"
    assert db.scalar(select(func.count(Client.id))) == 1


def test_reopening_a_closed_lead_is_refused_while_another_is_open(db) -> None:
    v, _ = _contacted_and_responded(db)
    ld = leads.convert(db, v.id, now=h.T0)
    leads.move_stage(db, ld.id, "lost", now=h.T0)
    second = leads.convert(db, v.id, now=h.T0)  # a prospect with a closed lead may get a new one
    assert second.id != ld.id
    with pytest.raises(DomainError) as ei:
        leads.move_stage(db, ld.id, "meeting_scheduled")
    assert ei.value.code == "lead_exists"


def test_lead_stage_and_field_validation(db) -> None:
    v, _ = _contacted_and_responded(db)
    ld = leads.convert(db, v.id, now=h.T0)
    with pytest.raises(DomainError) as ei:
        leads.move_stage(db, ld.id, "new")
    assert ei.value.code == "stage_invalid"
    with pytest.raises(DomainError) as ei2:
        leads.move_stage(db, ld.id, "meeting_scheduled", value_estimate=-1)
    assert ei2.value.code == "field_invalid"
    with pytest.raises(DomainError) as ei3:
        leads.move_stage(db, 99999, "won")
    assert ei3.value.http_status == 404


def test_list_and_get_lead(db) -> None:
    v, _ = _contacted_and_responded(db)
    ld = leads.convert(db, v.id, now=h.T0)
    page = leads.list_leads(db)
    assert page.total == 1 and page.items[0].id == ld.id and page.items[0].days_since_activity is not None
    assert leads.list_leads(db, stage="proposal_sent").total == 0
    assert leads.list_leads(db, q="acme").total == 1 and leads.list_leads(db, q="zzz").total == 0
    leads.move_stage(db, ld.id, "lost", now=h.T0)
    assert leads.list_leads(db).total == 0 and leads.list_leads(db, open_only=False).total == 1
    d = leads.get_lead(db, ld.id)
    assert d.timeline and d.enquiries and d.client is None


def test_client_from_lead_is_idempotent(db) -> None:
    v, _ = _contacted_and_responded(db)
    ld = leads.convert(db, v.id, now=h.T0)
    a = clients.create_from_lead(db, ld.id)
    b = clients.create_from_lead(db, ld.id)
    assert a.id == b.id
    with pytest.raises(DomainError):
        clients.create_from_lead(db, 99999)


def test_legacy_clients_update_list_and_detail(db) -> None:
    a = clients.create_legacy(db, name="Old Client Pvt Ltd", company="Old Client", started_on=date(2023, 4, 1), notes="2023 ML bootcamp")
    assert a.status == "legacy" and a.source == "manual" and a.started_on == date(2023, 4, 1)
    again = clients.create_legacy(db, name="old client pvt ltd", company="Old Client")
    assert again.id == a.id
    imp = clients.create_legacy(db, name="Imported Co", actor=IMPORT)
    assert imp.source == "legacy_import"
    with pytest.raises(DomainError) as ei:
        clients.create_legacy(db, name=" ")
    assert ei.value.code == "client_invalid"
    up = clients.update_client(db, a.id, name="Old Client Ltd", notes="renewed", status="active", actor=Actor("Priya"))
    assert up.name == "Old Client Ltd" and up.status == "active"
    with pytest.raises(DomainError):
        clients.update_client(db, a.id, status="bogus")
    assert clients.list_clients(db).total == 2 and clients.list_clients(db, status="legacy").total == 1
    assert clients.list_clients(db, q="imported").total == 1
    d = clients.get_client(db, a.id)
    assert d.notes == "renewed" and d.timeline and d.competitors == []
    with pytest.raises(DomainError):
        clients.get_client(db, 99999)


def test_enquiry_untouched_columns_survive_lead_flow(db) -> None:
    v, e = _contacted_and_responded(db)
    leads.convert(db, v.id, now=h.T0)
    db.refresh(e)
    assert db.get(Enquiry, e.id).review_status == "accepted"
