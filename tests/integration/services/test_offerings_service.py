"""Offerings catalogue on real SQLite: seed, create in new group, duplicates, delete vs deactivate."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from sales0.errors import DomainError
from sales0.services import offerings
from sales0.services.triage import active_offering_refs
from sales0.services.views import OfferingInput
from tests.integration.services.helpers import make_enquiry, set_triaged


def test_seeded_catalogue_lists_the_four_offerings_in_group_order(db: Session) -> None:
    rows = offerings.list_offerings(db)
    assert [(o.name, o.group_name) for o in rows] == [
        ("AI corporate workshops", "Training"),
        ("Technical AI training", "Training"),
        ("AI consulting & build services", "Services"),
        ("Products", "Products"),
    ]
    assert all(o.enquiry_count == 0 and o.active for o in rows)
    assert offerings.list_group_names(db) == ["Training", "Services", "Products"]


def test_create_offering_in_a_new_group_reaches_the_triage_catalogue(db: Session) -> None:
    created = offerings.create_offering(db, OfferingInput(name="AI audits", group_name="Services+", description="Audit"))
    assert created.group_name == "Services+" and created.active
    assert "Services+" in offerings.list_group_names(db)
    assert "AI audits" in [o.name for o in active_offering_refs(db)]


def test_create_offering_rejects_case_insensitive_duplicate_and_invalid_input(db: Session) -> None:
    with pytest.raises(DomainError) as dup:
        offerings.create_offering(db, OfferingInput(name="products", group_name="Products"))
    assert dup.value.code == "offering_duplicate" and dup.value.http_status == 409
    for bad in (
        OfferingInput(name="", group_name="Training"),
        OfferingInput(name="X", group_name=""),
        OfferingInput(name="n" * 81, group_name="Training"),
        OfferingInput(name="X", group_name="Training", description="d" * 501),
    ):
        with pytest.raises(DomainError) as info:
            offerings.create_offering(db, bad)
        assert info.value.code == "offering_invalid"


def test_update_offering_renames_and_moves_group_but_refuses_a_clash(db: Session) -> None:
    first = offerings.list_offerings(db)[0]
    moved = offerings.update_offering(db, first.id, OfferingInput(name="AI workshops", group_name="Services"))
    assert (moved.name, moved.group_name) == ("AI workshops", "Services")
    with pytest.raises(DomainError) as info:
        offerings.update_offering(db, first.id, OfferingInput(name="Products", group_name="Services"))
    assert info.value.code == "offering_duplicate"
    with pytest.raises(DomainError) as missing:
        offerings.update_offering(db, 999, OfferingInput(name="Z", group_name="Z"))
    assert missing.value.code == "not_found"


def test_delete_unused_offering_deletes_but_used_one_is_deactivated(db: Session) -> None:
    eid = make_enquiry("Acme wants an AI workshop")
    set_triaged(eid, offering_id=1)
    db.commit()
    assert offerings.delete_offering(db, 4) == "deleted"
    assert offerings.delete_offering(db, 1) == "deactivated"
    active = [o.id for o in offerings.list_offerings(db)]
    assert 1 not in active and 4 not in active
    everything = {o.id: o for o in offerings.list_offerings(db, include_inactive=True)}
    assert everything[1].active is False and everything[1].enquiry_count == 1
    assert 1 not in [o.id for o in active_offering_refs(db)]
    assert "Products" not in offerings.list_group_names(db)  # empty group hidden
    with pytest.raises(DomainError):
        offerings.delete_offering(db, 4)


def test_recreating_a_deactivated_offering_reactivates_it(db: Session) -> None:
    eid = make_enquiry("Acme wants an AI workshop")
    set_triaged(eid, offering_id=2)
    db.commit()
    assert offerings.delete_offering(db, 2) == "deactivated"
    again = offerings.create_offering(db, OfferingInput(name="Technical AI training", group_name="Training"))
    assert again.id == 2 and again.active
