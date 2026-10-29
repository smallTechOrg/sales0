"""CRM feedback slice (services): characteristics on THE entity (survive Prospect -> Lead -> Client), accept straight
to client, company matching / roll-up / merge, and editing a contact's e-mails / phones / LinkedIn."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import Characteristic, Client, Company, ContactEmail, Contact, EntityCharacteristic, Lead, Prospect, Suppression
from sales0.errors import DomainError
from sales0.services.crm import characteristics, clients, companies, contacts, leads, pipeline, prospects
from tests.integration.crm import helpers as h


def _trait(db: Session, key: str) -> int:
    row = db.scalar(select(Characteristic).where(Characteristic.system_key == key))
    assert row is not None
    return row.id


def _prospect(db: Session, **kw: object) -> int:
    e = h.enquiry(db, company=kw.pop("company", "Acme Training"), email=kw.pop("email", "asha@acme.example"),
                  contact_person="Asha Rao", **kw)
    return prospects.ensure_for_enquiry(db, e.id).id


# ---------------------------------------------------------------------------- B1 characteristics
def test_registry_seeded_create_rename_disable(db: Session) -> None:
    names = [c.name for c in characteristics.list_characteristics(db)]
    assert names == ["Training provider", "Partner", "Outreach client"]
    new = characteristics.create(db, name="  Reseller  ")
    assert new.name == "Reseller" and new.active
    with pytest.raises(DomainError):
        characteristics.create(db, name="reseller")
    renamed = characteristics.update(db, _trait(db, "partner"), name="Channel partner")
    assert renamed.name == "Channel partner" and renamed.system_key == "partner"  # the AI hint key survives a rename
    off = characteristics.update(db, new.id, active=False)
    assert not off.active
    assert "Reseller" not in [c.name for c in characteristics.list_characteristics(db, active_only=True)]


def test_characteristics_survive_prospect_lead_client(db: Session) -> None:
    pid = _prospect(db)
    tp, partner = _trait(db, "training_provider"), _trait(db, "partner")
    assert [c.name for c in characteristics.set_for(db, "prospect", pid, [tp, partner])] == ["Training provider", "Partner"]
    prospects.mark_contacted(db, pid)
    prospects.record_response(db, pid)
    lead = leads.convert(db, pid)
    assert {c.name for c in lead.characteristics} == {"Training provider", "Partner"}
    # editing from the lead edits the prospect's set (one entity)
    characteristics.set_for(db, "lead", lead.id, [partner])
    assert [c.name for c in prospects.get_prospect(db, pid).characteristics] == ["Partner"]
    leads.move_stage(db, lead.id, "won")
    client = db.scalar(select(Client).where(Client.lead_id == lead.id))
    assert client is not None
    assert [c.name for c in clients.get_client(db, client.id).characteristics] == ["Partner"]
    # only the prospect holds rows - nothing was copied
    rows = db.scalars(select(EntityCharacteristic)).all()
    assert [(r.entity_type, r.entity_id) for r in rows] == [("prospect", pid)]
    # timeline records the change on the prospect
    tl = prospects.get_prospect(db, pid).timeline
    assert any((t.body or "").startswith("Characteristics:") for t in tl)


def test_legacy_client_has_its_own_and_filters_work(db: Session) -> None:
    legacy = clients.create_legacy(db, name="Old Partner Co", company="Old Partner Co")
    partner, tp = _trait(db, "partner"), _trait(db, "training_provider")
    characteristics.set_for(db, "client", legacy.id, [partner])
    other = clients.create_legacy(db, name="Plain Client")
    pid = _prospect(db)
    characteristics.add_to(db, "prospect", pid, [tp])
    assert [c.id for c in clients.list_clients(db, characteristic_id=partner).items] == [legacy.id]
    assert other.id not in [c.id for c in clients.list_clients(db, characteristic_id=partner).items]
    assert [p.id for p in prospects.list_prospects(db, characteristic_id=tp).items] == [pid]
    assert prospects.list_prospects(db, characteristic_id=partner).items == []
    board = pipeline.board(db, kind="prospects", characteristic_id=tp)
    assert [c.id for col in board.columns for c in col.cards] == [pid]
    assert leads.list_leads(db, characteristic_id=tp).items == []
    page = companies.list_companies(db, characteristic_id=partner)
    assert [c.name for c in page.items] == ["Old Partner Co"] and [t.name for t in page.items[0].characteristics] == ["Partner"]


def test_retired_characteristic_cannot_be_added_but_stays(db: Session) -> None:
    pid = _prospect(db)
    partner = _trait(db, "partner")
    characteristics.set_for(db, "prospect", pid, [partner])
    characteristics.update(db, partner, active=False)
    assert [c.name for c in characteristics.set_for(db, "prospect", pid, [partner])] == ["Partner"]  # kept
    pid2 = _prospect(db, company="Beta Corp", email="b@beta.example")
    with pytest.raises(DomainError) as err:
        characteristics.set_for(db, "prospect", pid2, [partner])
    assert "no longer in use" in err.value.message
    with pytest.raises(DomainError):
        characteristics.set_for(db, "prospect", pid2, [99999])


def test_accept_choices_tick_and_go_straight_to_client(db: Session) -> None:
    e = h.enquiry(db, company="Gamma Learning", email="g@gamma.example", contact_person="Gita")
    tp = _trait(db, "training_provider")
    view = prospects.apply_accept_choices(db, e.id, characteristic_ids=[tp], as_client=True)
    p = db.get(Prospect, view.id)
    assert p is not None and p.status == "converted"
    lead = db.scalar(select(Lead).where(Lead.prospect_id == p.id))
    assert lead is not None and lead.stage == "won" and lead.closed_at is not None
    client = db.scalar(select(Client).where(Client.prospect_id == p.id))
    assert client is not None and client.lead_id == lead.id and client.source == "lead"
    assert [c.name for c in clients.get_client(db, client.id).characteristics] == ["Training provider"]
    # idempotent: a second accept with the same choices changes nothing
    prospects.apply_accept_choices(db, e.id, characteristic_ids=[tp], as_client=True)
    assert len(db.scalars(select(Client).where(Client.prospect_id == p.id)).all()) == 1
    assert len(db.scalars(select(EntityCharacteristic)).all()) == 1


# ---------------------------------------------------------------------------- B2 companies
def test_company_matching_by_domain_and_linkedin_not_generic_hosts(db: Session) -> None:
    a = companies.get_or_create_row(db, "Acme Learning Pvt Ltd", website="https://www.acme-learning.example/about")
    b = companies.get_or_create_row(db, "Acme Learning", website="acme-learning.example")
    assert a is not None and b is not None and a.id == b.id  # same domain -> same company
    c = companies.get_or_create_row(db, "Zed Co", linkedin_url="https://www.linkedin.com/company/zed-co/")
    d = companies.get_or_create_row(db, "ZED Company", linkedin_url="linkedin.com/company/Zed-Co")
    assert c is not None and d is not None and c.id == d.id
    g1 = companies.get_or_create_row(db, "Freelancer One", website="gmail.com")
    g2 = companies.get_or_create_row(db, "Freelancer Two", website="gmail.com")
    assert g1 is not None and g2 is not None and g1.id != g2.id


def test_company_rollup_and_list_counts(db: Session) -> None:
    pid = _prospect(db, company="Delta Skills")
    p = db.get(Prospect, pid)
    assert p is not None and p.company_id
    # a person linked to the prospect (no company of their own) still shows on the company page
    prospects.add_contact(db, pid, full_name="Dev Delta", email="dev@delta.example")
    co = companies.get_company(db, p.company_id)
    assert [x.id for x in co.prospects] == [pid]
    assert {c.email for c in co.contacts} >= {"asha@acme.example", "dev@delta.example"}
    assert co.enquiries and co.enquiries[0].prospect_id == pid
    row = companies.list_companies(db, q="delta").items[0]
    assert (row.prospects, row.enquiries) == (1, 1) and row.contacts >= 1
    prospects.mark_contacted(db, pid)
    assert companies.list_companies(db, q="delta").items[0].open_follow_ups == 3


def test_merge_companies_moves_everything(db: Session) -> None:
    pid = _prospect(db, company="Epsilon Edu")
    keep = db.get(Prospect, pid).company_id  # type: ignore[union-attr]
    dup = companies.get_or_create_row(db, "Epsilon Education", website="epsilon.example")
    assert dup is not None
    contacts.upsert(db, full_name="Eve", email="eve@epsilon.example", company_id=dup.id)
    pid2 = prospects.create_manual(db, name="Eve", company="Epsilon Education").id
    assert {c.id for c in companies.duplicate_candidates(db, keep)} == {dup.id}
    merged = companies.merge(db, keep, dup.id)
    assert merged.website == "epsilon.example"
    assert db.get(Company, dup.id) is None
    assert db.get(Prospect, pid2).company_id == keep  # type: ignore[union-attr]
    assert db.scalar(select(Contact.company_id).where(Contact.full_name == "Eve")) == keep
    assert any("Merged duplicate company" in (t.body or "") for t in companies.get_company(db, keep).timeline)
    with pytest.raises(DomainError):
        companies.merge(db, keep, keep)


def test_edit_prospect_company_fields_and_profile(db: Session) -> None:
    pid = _prospect(db, company="Zeta Labs")
    prospects.update(db, pid, website="zeta.example", company_linkedin_url="https://www.linkedin.com/company/zeta-labs/",
                     linkedin_url="https://www.linkedin.com/in/zeta-head/")
    p = db.get(Prospect, pid)
    co = db.get(Company, p.company_id)  # type: ignore[union-attr]
    assert co is not None and (co.domain_key, co.linkedin_key) == ("zeta.example", "company/zeta-labs")
    assert prospects.get_prospect(db, pid).linkedin_url == "https://www.linkedin.com/in/zeta-head/"
    with pytest.raises(DomainError):
        prospects.update(db, pid, company_linkedin_url="https://www.linkedin.com/in/not-a-company/")
    other = _prospect(db, company="Other Co", email="o@other.example")
    with pytest.raises(DomainError) as err:
        prospects.update(db, other, linkedin_url="linkedin.com/in/zeta-head")
    assert "already belongs to prospect" in err.value.message


# ---------------------------------------------------------------------------- B3 contacts
def test_contact_email_and_phone_editing(db: Session) -> None:
    cid = contacts.upsert(db, full_name="Priya Menon", email="priya@acme.example", phone="98765 43210").id
    contacts.add_email(db, cid, "Priya.M@Acme.example")
    detail = contacts.get_contact(db, cid)
    assert [(e.email, e.is_primary) for e in detail.email_items] == [("priya@acme.example", True), ("priya.m@acme.example", False)]
    second = detail.email_items[1].id
    view = contacts.set_primary_email(db, cid, second)
    assert view.email == "priya.m@acme.example"
    contacts.update_email(db, cid, second, "priya.menon@acme.example")
    assert contacts.get_contact(db, cid).email == "priya.menon@acme.example"
    contacts.remove_email(db, cid, second)
    after = contacts.get_contact(db, cid)
    assert after.email == "priya@acme.example" and [e.is_primary for e in after.email_items] == [True]
    with pytest.raises(DomainError):
        contacts.add_email(db, cid, "not-an-email")
    other = contacts.upsert(db, full_name="Raj", email="raj@acme.example").id
    with pytest.raises(DomainError) as err:
        contacts.add_email(db, other, "priya@acme.example")
    assert err.value.code == "contact_conflict"
    # phones
    contacts.add_phone(db, cid, "+44 20 7946 0958")
    phones = contacts.get_contact(db, cid).phone_items
    assert [p.is_primary for p in phones] == [True, False] and phones[0].phone == "+919876543210"
    contacts.set_primary_phone(db, cid, phones[1].id)
    assert contacts.get_contact(db, cid).phone == "+442079460958"
    contacts.remove_phone(db, cid, phones[1].id)
    assert contacts.get_contact(db, cid).phone == "+919876543210"
    with pytest.raises(DomainError):
        contacts.add_phone(db, cid, "98765 43210")  # already there
    # LinkedIn
    contacts.update_contact(db, cid, linkedin_url="https://www.linkedin.com/in/priya-menon/")
    assert contacts.get_contact(db, cid).linkedin_url == "https://www.linkedin.com/in/priya-menon/"
    with pytest.raises(DomainError):
        contacts.update_contact(db, other, linkedin_url="linkedin.com/in/priya-menon")
    # the change is on the timeline but the value is not
    bodies = [t.body or "" for t in contacts.get_contact(db, cid).timeline]
    assert "E-mail added" in bodies and not any("priya.menon@" in b for b in bodies)


def test_suppressed_address_is_flagged_on_the_contact(db: Session) -> None:
    cid = contacts.upsert(db, full_name="Sam", email="sam@nope.example").id
    db.add(Suppression(address="sam@nope.example", reason="unsubscribe"))
    db.flush()
    assert [e.suppressed for e in contacts.get_contact(db, cid).email_items] == [True]
    assert db.scalar(select(ContactEmail.email).where(ContactEmail.contact_id == cid)) == "sam@nope.example"
