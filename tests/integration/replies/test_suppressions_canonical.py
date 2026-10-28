from __future__ import annotations

import pytest

from sales0.errors import DomainError
from sales0.services import suppressions
from sales0.services.suppressions import canonical


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("Bob@Example.COM", "bob@example.com"),
        ("bob+news@example.com", "bob@example.com"),
        ("<B.o.b+x@Gmail.com>", "bob@gmail.com"),
        ("b.ob@googlemail.com", "bob@gmail.com"),
        ("first.last@example.com", "first.last@example.com"),  # dots kept off gmail
        ("*@Example.com", "@example.com"),
        ("@example.com", "@example.com"),
    ],
)
def test_canonical(raw: str, want: str) -> None:
    assert canonical(raw) == want


def test_variants_are_suppressed_and_add_is_idempotent(db) -> None:  # type: ignore[no-untyped-def]
    a = suppressions.add(db, "bob@gmail.com", "manual")
    assert suppressions.add(db, "B.O.B+spam@googlemail.com", "manual").id == a.id
    assert suppressions.suppressed_among(db, "b.o.b+zzz@gmail.com", "other@gmail.com") == ["b.o.b+zzz@gmail.com"]


def test_legacy_noncanonical_rows_match_on_compare(db) -> None:  # type: ignore[no-untyped-def]
    from sales0.db.models import Suppression

    db.add(Suppression(address="a.b+t@gmail.com", reason="manual"))
    db.flush()
    assert suppressions.suppressed_among(db, "ab@gmail.com") == ["ab@gmail.com"]


def test_domain_level_entry(db) -> None:  # type: ignore[no-untyped-def]
    row = suppressions.add(db, "*@Example.com", "manual")
    assert row.address == "@example.com"
    assert suppressions.suppressed_among(db, "x@example.com, y@other.com") == ["x@example.com"]
    with pytest.raises(DomainError) as exc:
        suppressions.assert_not_suppressed(db, "anyone+tag@example.com")
    assert exc.value.code == "recipient_suppressed"
    suppressions.assert_not_suppressed(db, "x@sub.example.com")  # exact domain only


def test_invalid_entries_rejected(db) -> None:  # type: ignore[no-untyped-def]
    for bad in ("", "nodomain", "@", "x@"):
        with pytest.raises(DomainError):
            suppressions.add(db, bad, "manual")
