"""Deck (PDF) library: validated upload, content-addressed storage, lookup by offering, soft delete.

The PDF lives in the attachment store (``storage.py``: local disk, or Google Cloud Storage when hosted) under
``decks/<sha256>.pdf``, stored once per content. The ``decks`` row holds only metadata; messages reference the deck by
id and their approval hash covers the bytes, so nothing is copied per draft / approval / send.
"""

from __future__ import annotations

import hashlib
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.config import Settings, get_settings
from sales0.db.models import Deck
from sales0.errors import DomainError
from sales0.services._common import not_found
from sales0.services.outreach import storage

PDF_MIME = "application/pdf"
_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(filename: str | None) -> str:
    """Basename only, conservative charset, always ends in .pdf, max 120 chars."""
    base = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = base[:-4] if base.lower().endswith(".pdf") else base
    stem = _UNSAFE.sub("_", stem).strip(" ._-")[:110]
    return f"{stem or 'deck'}.pdf"


def upload_deck(
    db: Session,
    *,
    filename: str | None,
    data: bytes,
    name: str | None = None,
    offering_id: int | None = None,
    settings: Settings | None = None,
) -> Deck:
    """Store a PDF as ``decks/<sha256>.pdf`` in the attachment store (once per content) + a metadata row. The same
    bytes uploaded again return the existing row (re-activated if it had been deleted, a lost file restored; its offering is updated when one is given)."""
    settings = settings or get_settings()
    if not data:
        raise DomainError("deck_invalid", "That file is empty.", 400)
    if len(data) > settings.deck_max_bytes:
        raise DomainError(
            "deck_too_large",
            f"That file is {len(data) / 1024 / 1024:.1f} MB; the limit is {settings.deck_max_bytes / 1024 / 1024:.0f} MB.",
            413,
        )
    if not data.startswith(b"%PDF"):
        raise DomainError("deck_not_pdf", "Only PDF files can be uploaded as decks.", 400)
    sha = hashlib.sha256(data).hexdigest()
    clean = safe_filename(filename)
    display = " ".join((name or clean[:-4]).split())[:120] or "Deck"

    backend = storage.current_backend(settings)  # hosted without a bucket: refused loudly, never /tmp
    store = storage.store_for(backend, settings)
    key = storage.deck_key(sha)
    if not store.exists(key):
        store.put(key, data)
    existing = db.execute(select(Deck).where(Deck.sha256 == sha)).scalars().first()
    if existing is not None:
        existing.active = True
        if offering_id is not None:
            existing.offering_id = offering_id
        existing.storage, existing.stored_path, existing.content_missing = backend, key, False  # heals a lost file
        db.flush()
        return existing

    row = Deck(
        offering_id=offering_id, name=display, filename=clean, stored_path=key, sha256=sha,
        size_bytes=len(data), active=True, storage=backend, content_missing=False,
    )  # fmt: skip
    db.add(row)
    db.flush()
    return row


def get_deck(db: Session, deck_id: int) -> Deck:
    row = db.get(Deck, deck_id)
    if row is None:
        raise not_found()
    return row


def list_decks(db: Session, *, include_inactive: bool = False) -> list[Deck]:
    stmt = select(Deck).order_by(Deck.name, Deck.id)
    if not include_inactive:
        stmt = stmt.where(Deck.active.is_(True))
    return list(db.execute(stmt).scalars())


def deck_for_offering(db: Session, offering_id: int | None) -> Deck | None:
    """The newest active deck mapped to the offering, else the newest generic (no offering) deck. Decks whose file is
    missing are never picked by default (they could not be attached)."""
    usable = (Deck.active.is_(True), Deck.content_missing.is_(False))
    if offering_id is not None:
        row = db.execute(
            select(Deck).where(Deck.offering_id == offering_id, *usable).order_by(Deck.id.desc())
        ).scalars().first()
        if row is not None:
            return row
    return db.execute(
        select(Deck).where(Deck.offering_id.is_(None), *usable).order_by(Deck.id.desc())
    ).scalars().first()


def missing_for_offering(db: Session, offering_id: int | None) -> Deck | None:
    """An active deck mapped to the offering whose file is missing (so the UI can say "upload it again")."""
    if offering_id is None:
        return None
    return db.execute(
        select(Deck).where(Deck.offering_id == offering_id, Deck.active.is_(True), Deck.content_missing.is_(True))
        .order_by(Deck.id.desc())
    ).scalars().first()


def set_deck_offering(db: Session, deck_id: int, offering_id: int | None) -> Deck:
    row = get_deck(db, deck_id)
    row.offering_id = offering_id
    db.flush()
    return row


def delete_deck(db: Session, deck_id: int) -> None:
    """Soft delete: the file stays (messages already approved with it must still send)."""
    get_deck(db, deck_id).active = False
    db.flush()


def _store_of(deck: Deck, settings: Settings | None = None) -> storage.AttachmentStore | None:
    """The deck's store; None when it can no longer exist (a ``local`` row on the hosted app: its /tmp was wiped)."""
    settings = settings or get_settings()
    if deck.storage == "local" and settings.mode == "hosted":
        return None
    return storage.store_for(deck.storage or "local", settings)


def is_available(deck: Deck, settings: Settings | None = None) -> bool:
    """True when the deck's file exists in its store (one HEAD / stat; never downloads)."""
    if deck.content_missing:
        return False
    store = _store_of(deck, settings)
    return store is not None and store.exists(deck.stored_path)


def read_deck_bytes(deck: Deck, settings: Settings | None = None) -> bytes:
    """The PDF from its store, verified against the recorded sha256. A missing file sets ``content_missing`` and raises
    ``deck_missing`` (409, "file missing - upload it again"); altered bytes raise ``deck_corrupt``."""
    store = _store_of(deck, settings)
    data = store.get(deck.stored_path) if store is not None else None
    if data is None:
        deck.content_missing = True
        raise DomainError(
            "deck_missing",
            f"The deck file '{deck.filename}' is missing (file missing - upload it again on Outreach > Decks).",
            409,
        )
    if hashlib.sha256(data).hexdigest() != deck.sha256:
        raise DomainError("deck_corrupt", f"The stored deck '{deck.filename}' does not match its hash - upload it again.", 409)
    return data


def attachment_label(deck: Deck | None) -> str:
    """"name.pdf (123 KB)" for the UI, or "none"."""
    if deck is None:
        return "none"
    kb = deck.size_bytes / 1024
    size = f"{kb / 1024:.1f} MB" if kb >= 1024 else f"{max(1, round(kb))} KB"
    return f"{deck.filename} ({size})"
