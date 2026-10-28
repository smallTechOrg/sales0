"""Template CRUD/validation and the deck library rules, on a real migrated DB."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from sales0.config import reset_settings
from sales0.errors import DomainError
from sales0.services.outreach import decks, templates
from tests.integration.outreach.conftest import PDF


def test_template_crud_roundtrip_and_offering_fallback(db: Session, offering) -> None:
    generic = templates.create_template(db, name="Generic", subject="Hi {first_name}", body="About {company}: {requirement}")
    specific = templates.create_template(
        db, name="Workshops", subject="{offering} for {company}", body="Dear {first_name}, from {sender_name}", offering_id=offering.id
    )
    db.commit()
    assert templates.template_for_offering(db, offering.id).id == specific.id
    assert templates.template_for_offering(db, 9999).id == generic.id
    assert templates.template_for_offering(db, None).id == generic.id

    templates.update_template(db, specific.id, name="Workshops v2", subject="S", body="B {company}", offering_id=offering.id)
    assert templates.get_template(db, specific.id).name == "Workshops v2"
    templates.delete_template(db, specific.id)
    db.commit()
    assert [t.id for t in templates.list_templates(db)] == [generic.id]
    assert {t.id for t in templates.list_templates(db, include_inactive=True)} == {generic.id, specific.id}
    assert templates.template_for_offering(db, offering.id).id == generic.id  # soft-deleted one is skipped


def test_template_with_unknown_placeholder_or_empty_body_is_refused(db: Session) -> None:
    with pytest.raises(DomainError) as exc:
        templates.create_template(db, name="Bad", subject="Hi", body="Price is {price}")
    assert exc.value.code == "template_invalid" and "{price}" in exc.value.message
    with pytest.raises(DomainError):
        templates.create_template(db, name="Empty", subject="Hi", body="   ")
    with pytest.raises(DomainError):
        templates.update_template(db, 12345, name="x", subject="", body="b")  # not found


def test_deck_rejects_non_pdf_empty_and_oversize(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    for data, code in ((b"<html>not a pdf</html>", "deck_not_pdf"), (b"", "deck_invalid")):
        with pytest.raises(DomainError) as exc:
            decks.upload_deck(db, filename="x.pdf", data=data)
        assert exc.value.code == code
    monkeypatch.setenv("SALES0_DECK_MAX_BYTES", "100")
    reset_settings()
    with pytest.raises(DomainError) as exc:
        decks.upload_deck(db, filename="big.pdf", data=PDF + b"0" * 200)
    assert exc.value.code == "deck_too_large" and exc.value.http_status == 413
    assert decks.list_decks(db) == []


def test_deck_storage_dedupe_sanitising_lookup_and_soft_delete(db: Session, offering) -> None:
    a = decks.upload_deck(db, filename="../../etc/we!rd name?.PDF", data=PDF, offering_id=offering.id)
    db.commit()
    assert a.filename == "we_rd name.pdf" and a.stored_path == f"decks/{a.sha256}.pdf"
    from sales0.config import get_settings

    stored = get_settings().data_path / a.stored_path
    assert stored.read_bytes() == PDF
    assert decks.safe_filename("") == "deck.pdf" and decks.safe_filename("a/b\\c.txt") == "c.txt.pdf"

    b = decks.upload_deck(db, filename="again.pdf", data=PDF)  # same bytes -> same row
    assert b.id == a.id and len(decks.list_decks(db)) == 1
    assert [p.name for p in stored.parent.iterdir()] == [f"{a.sha256}.pdf"]

    assert decks.deck_for_offering(db, offering.id).id == a.id
    other = decks.upload_deck(db, filename="generic.pdf", data=PDF + b"%different")
    db.commit()
    assert decks.deck_for_offering(db, 999).id == other.id  # generic fallback

    decks.delete_deck(db, a.id)
    db.commit()
    assert decks.deck_for_offering(db, offering.id).id == other.id
    assert decks.upload_deck(db, filename="x.pdf", data=PDF).active  # re-upload revives it

    stored.write_bytes(PDF + b"tampered")
    with pytest.raises(DomainError) as exc:
        decks.read_deck_bytes(a)
    assert exc.value.code == "deck_corrupt"
