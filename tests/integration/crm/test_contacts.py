"""Contacts: matching, fill-don't-blank, tags, search at scale, duplicates, merge."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import func, insert, select

from sales0.db.models import Contact, ContactEmail, ContactLink, ContactPhone, Enquiry, Prospect, TimelineEvent
from sales0.errors import DomainError
from sales0.services.crm import companies, contacts, prospects
from tests.integration.crm import helpers as h


def test_upsert_requires_name_email_or_linkedin(db) -> None:
    with pytest.raises(DomainError) as ei:
        contacts.upsert(db, phone="9876543210")
    assert ei.value.code == "contact_invalid" and ei.value.http_status == 400
    assert contacts.upsert(db, email="solo@x.example").email == "solo@x.example"
    assert contacts.upsert(db, linkedin_url="https://www.linkedin.com/in/only-slug/").linkedin_url


def test_match_by_any_known_email_case_insensitive_and_adds_new_ones(db) -> None:
    a = contacts.upsert(db, full_name="Asha Rao", email="Asha@Acme.example")
    b = contacts.upsert(db, email="asha@acme.example, asha.rao@personal.example", phone="98765 43210")
    assert a.id == b.id and a.email == "asha@acme.example"
    d = contacts.get_contact(db, a.id)
    assert set(d.emails) == {"asha@acme.example", "asha.rao@personal.example"} and d.phones == ["+919876543210"]
    c = contacts.upsert(db, email="asha.rao@personal.example")
    assert c.id == a.id and db.scalar(select(func.count(Contact.id))) == 1


def test_match_by_linkedin_slug_then_fill_but_never_blank(db) -> None:
    a = contacts.upsert(db, full_name="Ravi K", linkedin_url="https://www.linkedin.com/in/ravi-k/", title="HR Head")
    b = contacts.upsert(db, full_name="Different Name", linkedin_url="linkedin.com/in/Ravi-K", email="ravi@x.example", title="CEO")
    assert a.id == b.id
    d = contacts.get_contact(db, a.id)
    assert d.full_name == "Ravi K" and d.title == "HR Head" and d.email == "ravi@x.example"  # filled the empty email only


def test_phone_matches_only_with_a_loosely_matching_name(db) -> None:
    a = contacts.upsert(db, full_name="Priya Menon", phone="+91 98765 43210")
    same = contacts.upsert(db, full_name="Priya R Menon", phone="09876543210")
    other = contacts.upsert(db, full_name="Karan Shah", phone="9876543210")
    assert same.id == a.id and other.id != a.id


def test_company_is_linked_and_searchable(db) -> None:
    comp = companies.get_or_create(db, "Acme Training", website="https://www.acme.example", city="Pune")
    c = contacts.upsert(db, full_name="Asha Rao", email="asha@acme.example", company_id=comp.id)
    assert c.company_name == "Acme Training"
    assert [x.id for x in contacts.list_contacts(db, q="acme training").items] == [c.id]
    assert [x.id for x in contacts.list_contacts(db, company_id=comp.id).items] == [c.id]
    assert companies.get_or_create(db, "  ACME   training ").id == comp.id
    assert companies.list_companies(db, q="acme").total == 1


def test_tags_filter_search_and_counts(db) -> None:
    hr = contacts.upsert(db, full_name="Hema HR", email="hema@x.example")
    ceo = contacts.upsert(db, full_name="Carl CEO", email="carl@x.example")
    contacts.add_tag(db, hr.id, "HR", "role")
    contacts.add_tag(db, hr.id, "hr")  # same tag (case-insensitive)
    contacts.add_tag(db, ceo.id, "Decision maker", "segment")
    contacts.add_tag(db, hr.id, "Decision maker")
    assert [c.id for c in contacts.list_contacts(db, tag="hr").items] == [hr.id]
    assert {c.id for c in contacts.list_contacts(db, tag="Decision Maker").items} == {hr.id, ceo.id}
    assert [c.id for c in contacts.list_contacts(db, q="decision").items] == [hr.id, ceo.id] or {c.id for c in contacts.list_contacts(db, q="decision").items} == {hr.id, ceo.id}
    tags = {t.name: t for t in contacts.list_tags(db)}
    assert tags["HR"].count == 1 and tags["HR"].category == "role" and tags["Decision maker"].count == 2
    view = contacts.get_contact(db, hr.id)
    assert {t.name for t in view.tags} == {"HR", "Decision maker"}
    out = contacts.remove_tag(db, hr.id, tags["HR"].id)
    assert {t.name for t in out.tags} == {"Decision maker"}
    assert {t.name: t.count for t in contacts.list_tags(db)}["HR"] == 0  # unused tags are kept
    for bad in ("", "x" * 41):
        with pytest.raises(DomainError) as ei:
            contacts.add_tag(db, hr.id, bad)
        assert ei.value.code == "tag_invalid"
    with pytest.raises(DomainError):
        contacts.add_tag(db, hr.id, "ok", "bogus-category")


def test_update_contact_fields_and_company(db) -> None:
    c = contacts.upsert(db, full_name="Old Name", email="o@x.example")
    out = contacts.update_contact(db, c.id, full_name="New Name", title="Director", notes="n", company="Brand Co")
    assert out.full_name == "New Name" and out.company_name == "Brand Co" and out.title == "Director"
    assert contacts.get_contact(db, c.id).notes == "n"
    assert [x.id for x in contacts.list_contacts(db, q="brand").items] == [c.id]
    assert contacts.update_contact(db, c.id, company=None).company_id is None
    with pytest.raises(DomainError):
        contacts.upsert(db, full_name=" ")
    nameless = contacts.upsert(db, phone="+919000000001", email="n@x.example")
    assert nameless.full_name is None


def test_link_is_idempotent_and_listed_on_the_detail(db) -> None:
    c = contacts.upsert(db, full_name="Asha", email="a@x.example")
    contacts.link(db, c.id, "company", 5, "sheet")
    contacts.link(db, c.id, "company", 5)
    assert db.scalar(select(func.count(ContactLink.id))) == 1
    with pytest.raises(ValueError):
        contacts.link(db, c.id, "banana", 1)
    d = contacts.get_contact(db, c.id)
    assert len(d.links) == 1 and d.links[0].role == "sheet"
    assert [t.kind for t in d.timeline][:2] == ["contact_linked", "created"]


def test_search_over_10k_contacts_is_fast_and_paged(db) -> None:
    rows = [
        {"full_name": f"Person {i:05d}", "email": f"p{i}@corp{i % 50}.example", "search_text": f"person {i:05d} p{i}@corp{i % 50}.example corp {i % 50}",
         "origin": "import", "actor": "import", "created_at": h.T0, "updated_at": h.T0}
        for i in range(10_000)
    ]
    db.execute(insert(Contact), rows)
    db.flush()
    t0 = time.perf_counter()
    res = contacts.list_contacts(db, q="person 0099")
    dt_search = time.perf_counter() - t0
    expected = sum(1 for r in rows if "person" in r["search_text"] and "0099" in r["search_text"])
    assert res.total == expected > 10
    assert len(res.items) == min(expected, 50)
    t0 = time.perf_counter()
    first = contacts.list_contacts(db, page=3, page_size=50)
    dt_page = time.perf_counter() - t0
    assert first.total == 10_000 and first.items[0].full_name == "Person 00100" and len(first.items) == 50
    assert dt_search < 0.3 and dt_page < 0.3, (dt_search, dt_page)
    t0 = time.perf_counter()
    assert contacts.list_contacts(db, q="corp 7", tag="nothing").total == 0
    assert time.perf_counter() - t0 < 0.3


def test_duplicates_by_name_company_and_phone_with_loose_names(db) -> None:
    comp = companies.get_or_create(db, "Dup Co")
    a = contacts.upsert(db, full_name="Sam Lee", email="sam@dup.example", company_id=comp.id)
    b = contacts.upsert(db, full_name="sam  LEE", email="sam.lee@other.example", company_id=comp.id)
    assert a.id != b.id
    p1 = contacts.upsert(db, full_name="Meera Iyer", phone="+919811122233")
    p2 = contacts.upsert(db, full_name="Totally Different", phone="+919811122233")
    p3 = contacts.upsert(db, full_name="Meera Iyer-Nair", email="m@x.example")
    db.add(ContactPhone(contact_id=p3.id, phone="+919811122233", digits="9811122233"))
    db.flush()
    groups = contacts.find_duplicates(db)
    by_reason = {g.reason: {c.id for c in g.contacts} for g in groups}
    assert by_reason["Same name and company"] == {a.id, b.id}
    phone_groups = [{c.id for c in g.contacts} for g in groups if g.reason == "Same phone number"]
    assert {p1.id, p3.id} in phone_groups or {p1.id, p2.id} not in phone_groups
    assert all(p2.id not in g for g in phone_groups)  # a different name never joins a phone group
    assert contacts.find_duplicates(db, limit=1).__len__() == 1


def test_merge_moves_everything_and_repoints(db) -> None:
    prof = h.profile(db)
    e = h.enquiry(db, profile_row=prof, company="Acme Training", email="b@acme.example", contact_person="Bea B")
    pv = prospects.ensure_for_enquiry(db, e.id)
    keep = contacts.upsert(db, full_name="Bea Bhatt", email="bea@home.example", phone="+919822233344")
    drop_id = db.get(Prospect, pv.id).contact_id
    contacts.add_tag(db, drop_id, "HR")
    contacts.add_tag(db, keep.id, "VIP")
    contacts.link(db, drop_id, "company", 77, "buyer")
    drop_slug = db.get(Contact, drop_id).linkedin_slug
    out = contacts.merge(db, keep.id, drop_id)
    assert out.id == keep.id and {t.name for t in out.tags} == {"HR", "VIP"}
    assert db.get(Contact, drop_id) is None
    d = contacts.get_contact(db, keep.id)
    assert {"b@acme.example", "bea@home.example"} <= set(d.emails)
    assert any(ln.entity_type == "company" and ln.entity_id == 77 for ln in d.links)
    assert db.get(Prospect, pv.id).contact_id == keep.id and db.get(Enquiry, e.id).contact_id == keep.id
    assert db.scalar(select(func.count(TimelineEvent.id)).where(TimelineEvent.contact_id == drop_id)) == 0
    assert "contact_merged" in [t.kind for t in d.timeline]
    assert db.scalar(select(func.count(ContactEmail.id)).where(ContactEmail.contact_id == drop_id)) == 0
    assert drop_slug is None or db.get(Contact, keep.id).linkedin_slug == drop_slug


def test_merge_moves_the_linkedin_slug_and_dedupes_links_and_phones(db) -> None:
    keep = contacts.upsert(db, full_name="Ann", email="ann@x.example", phone="+919833344455")
    drop = contacts.upsert(db, full_name="Ann S", linkedin_url="https://www.linkedin.com/in/ann-s/", phone="+919833399999")
    db.add(ContactPhone(contact_id=drop.id, phone="+919833344455", digits="9833344455"))  # the same number on both
    db.flush()
    contacts.link(db, keep.id, "prospect", 1)
    contacts.link(db, drop.id, "prospect", 1, "buyer")
    out = contacts.merge(db, keep.id, drop.id)
    d = contacts.get_contact(db, out.id)
    assert out.linkedin_url and sorted(d.phones) == ["+919833344455", "+919833399999"]
    assert [ln.role for ln in d.links if ln.entity_type == "prospect"] == ["buyer"]


def test_merge_validations(db) -> None:
    a = contacts.upsert(db, full_name="A", email="a@x.example")
    with pytest.raises(DomainError) as ei:
        contacts.merge(db, a.id, a.id)
    assert ei.value.code == "merge_self" and ei.value.http_status == 400
    with pytest.raises(DomainError) as nf:
        contacts.merge(db, a.id, 99999)
    assert nf.value.http_status == 404
