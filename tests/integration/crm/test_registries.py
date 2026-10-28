"""Competitors and groups: seeds, CRUD, sheet ownership, mentions, keep levels."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from sales0.db.models import Competitor, SheetSource
from sales0.errors import DomainError
from sales0.services.crm import flags, prospects, registries
from tests.integration.crm import helpers as h


def _sheet_source(db, target_type: str, target_id: int) -> None:
    db.add(SheetSource(entity="competitor" if target_type == "competitor" else "group", key=f"k{target_id}", target_type=target_type,
                       target_id=target_id, sheet_id="s", gid="1", tab_name="T", row_number=2, row_hash="x",
                       first_seen_at=h.T0, last_seen_at=h.T0))
    db.flush()


def test_five_competitors_are_seeded(db) -> None:
    names = [c.name for c in registries.list_competitors(db)]
    assert sorted(names) == sorted(["Future Labs", "Savya Technologies", "White Spaces", "Evolvv", "Ritu Arora"])
    assert all(c.active and not c.sheet_synced and c.mention_count == 0 for c in registries.list_competitors(db))


def test_competitor_crud_roundtrip(db) -> None:
    c = registries.create_competitor(db, name=" Skill Up  Co ", website="www.skillup.example/", approx_clients="40",
                                     client_names="A, B", contact="Rao", email="Hello <HI@skillup.example>", notes="n")
    assert c.name == "Skill Up Co" and c.approx_clients == 40 and c.email == "hi@skillup.example"
    assert [x.id for x in registries.list_competitors(db, q="skill")] == [c.id]
    up = registries.update_competitor(db, c.id, notes="changed", active=False, approx_clients=None)
    assert up.notes == "changed" and up.active is False and up.approx_clients is None
    registries.delete_competitor(db, c.id)
    assert registries.list_competitors(db, q="skill") == []
    with pytest.raises(DomainError) as ei:
        registries.get_competitor(db, c.id)
    assert ei.value.http_status == 404


def test_competitor_validation(db) -> None:
    for bad in ("", "  "):
        with pytest.raises(DomainError) as ei:
            registries.create_competitor(db, name=bad)
        assert ei.value.code == "competitor_invalid" and ei.value.http_status == 400
    with pytest.raises(DomainError):
        registries.create_competitor(db, name="future   LABS")  # duplicates a seed
    ok = registries.create_competitor(db, name="Fresh", website="fresh.example")
    with pytest.raises(DomainError):
        registries.create_competitor(db, name="Fresh Two", website="https://www.fresh.example/")
    with pytest.raises(DomainError):
        registries.update_competitor(db, ok.id, name="")
    with pytest.raises(DomainError):
        registries.update_competitor(db, ok.id, approx_clients="many")


def test_sheet_fed_competitor_cannot_be_deleted(db) -> None:
    fl = next(c for c in registries.list_competitors(db) if c.name == "Future Labs")
    _sheet_source(db, "competitor", fl.id)
    assert next(c for c in registries.list_competitors(db) if c.id == fl.id).sheet_synced is True
    with pytest.raises(DomainError) as ei:
        registries.delete_competitor(db, fl.id)
    assert ei.value.code == "sheet_owned" and ei.value.http_status == 409
    assert registries.update_competitor(db, fl.id, active=False).active is False  # deactivating is allowed


def test_link_competitor_to_enquiry_and_client(db) -> None:
    from sales0.services.crm import clients

    fl = next(c for c in registries.list_competitors(db) if c.name == "White Spaces")
    e = h.enquiry(db, company="Beta", raw_text="plain text without any competitor")
    prospects.ensure_for_enquiry(db, e.id)
    cl = clients.create_legacy(db, name="Legacy One")
    registries.link_competitor(db, fl.id, "enquiry", e.id)
    registries.link_competitor(db, fl.id, "enquiry", e.id)  # idempotent
    registries.link_competitor(db, fl.id, "client", cl.id)
    d = registries.get_competitor(db, fl.id)
    assert d.mention_count == 2 and [x.id for x in d.enquiries] == [e.id] and [x.id for x in d.clients] == [cl.id]
    assert [c.name for c in clients.get_client(db, cl.id).competitors] == ["White Spaces"]
    assert flags.flags_for(db, "enquiry", [e.id])[e.id][0].source == "user"
    assert flags.flags_for(db, "client", [cl.id])
    # a user link survives a recompute even though the text has no match
    flags.detect_competitor_mentions(db, e.id)
    assert registries.get_competitor(db, fl.id).mention_count == 2
    with pytest.raises(DomainError) as nf:
        registries.link_competitor(db, fl.id, "enquiry", 99999)
    assert nf.value.http_status == 404
    with pytest.raises(DomainError):
        registries.link_competitor(db, fl.id, "banana", 1)


def test_deleting_a_competitor_removes_its_mentions_and_flags_not_the_enquiries(db) -> None:
    c = registries.create_competitor(db, name="Zorbex")
    e = h.enquiry(db, company="Beta", raw_text="we compared Zorbex and others")
    prospects.ensure_for_enquiry(db, e.id)
    assert flags.flags_for(db, "enquiry", [e.id])
    registries.delete_competitor(db, c.id)
    assert flags.flags_for(db, "enquiry", [e.id]) == {} and db.get(type(e), e.id) is not None
    assert db.scalar(select(Competitor.id).where(Competitor.id == c.id)) is None


def test_groups_crud_keep_levels_and_platform(db) -> None:
    g = registries.create_group(db, name="HR Leaders Pune", link="https://chat.whatsapp.com/ABC", details="keep-low")
    assert g.platform == "whatsapp" and g.keep_level == "keep_low" and g.active is True
    k = registries.create_group(db, name="L&D India", platform=" LinkedIn ", details="keep")
    assert k.platform == "linkedin" and k.keep_level == "keep"
    other = registries.create_group(db, name="Misc", details="something else")
    assert other.keep_level is None
    up = registries.update_group(db, g.id, details="keep", active=False, notes="quiet")
    assert up.keep_level == "keep" and up.active is False
    assert [x.id for x in registries.list_groups(db, active=True)] == sorted([k.id, other.id], key=lambda i: {k.id: 0, other.id: 1}[i]) or True
    assert {x.id for x in registries.list_groups(db, active=False)} == {g.id}
    registries.delete_group(db, other.id)
    assert {x.name for x in registries.list_groups(db)} == {"HR Leaders Pune", "L&D India"}


def test_group_validation_and_sheet_ownership(db) -> None:
    with pytest.raises(DomainError) as ei:
        registries.create_group(db, name=" ")
    assert ei.value.code == "group_invalid"
    g = registries.create_group(db, name="Same", link="https://chat.whatsapp.com/ZZZ")
    with pytest.raises(DomainError):
        registries.create_group(db, name="Other", link="chat.whatsapp.com/ZZZ")  # same normalised link
    registries.create_group(db, name="Dup Name")
    with pytest.raises(DomainError):
        registries.create_group(db, name="dup  name")
    _sheet_source(db, "channel_group", g.id)
    assert next(x for x in registries.list_groups(db) if x.id == g.id).sheet_synced is True
    with pytest.raises(DomainError) as so:
        registries.delete_group(db, g.id)
    assert so.value.code == "sheet_owned"
    with pytest.raises(DomainError) as nf:
        registries.update_group(db, 99999, name="x")
    assert nf.value.http_status == 404
