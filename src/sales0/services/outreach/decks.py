"""Deck (PDF) library: validated upload, content-addressed storage, lookup by offering, soft delete."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.config import Settings, get_settings
from sales0.db.models import Deck
from sales0.errors import DomainError
from sales0.services._common import not_found

PDF_MIME = "application/pdf"
_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(filename: str | None) -> str:
    """Basename only, conservative charset, always ends in .pdf, max 120 chars."""
    base = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = base[:-4] if base.lower().endswith(".pdf") else base
    stem = _UNSAFE.sub("_", stem).strip(" ._-")[:110]
    return f"{stem or 'deck'}.pdf"


def decks_dir(settings: Settings | None = None) -> Path:
    path = (settings or get_settings()).data_path / "decks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def upload_deck(
    db: Session,
    *,
    filename: str | None,
    data: bytes,
    name: str | None = None,
    offering_id: int | None = None,
    settings: Settings | None = None,
) -> Deck:
    """Store a PDF as ``<data_dir>/decks/<sha256>.pdf``. The same bytes uploaded again return the
    existing row (re-activated if it had been deleted; its offering is updated when one is given)."""
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

    existing = db.execute(select(Deck).where(Deck.sha256 == sha)).scalars().first()
    if existing is not None:
        existing.active = True
        if offering_id is not None:
            existing.offering_id = offering_id
        _ensure_file(settings, sha, data)
        db.flush()
        return existing

    stored = _ensure_file(settings, sha, data)
    row = Deck(
        offering_id=offering_id, name=display, filename=clean, stored_path=stored, sha256=sha,
        size_bytes=len(data), active=True,
    )  # fmt: skip
    db.add(row)
    db.flush()
    return row


def _ensure_file(settings: Settings, sha: str, data: bytes) -> str:
    rel = f"decks/{sha}.pdf"
    target = decks_dir(settings) / f"{sha}.pdf"
    if not target.exists():
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    return rel


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
    """The newest active deck mapped to the offering, else the newest generic (no offering) deck."""
    if offering_id is not None:
        row = db.execute(
            select(Deck).where(Deck.offering_id == offering_id, Deck.active.is_(True)).order_by(Deck.id.desc())
        ).scalars().first()
        if row is not None:
            return row
    return db.execute(
        select(Deck).where(Deck.offering_id.is_(None), Deck.active.is_(True)).order_by(Deck.id.desc())
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


def read_deck_bytes(deck: Deck, settings: Settings | None = None) -> bytes:
    """The stored PDF, verified against its recorded hash (DomainError if missing or altered)."""
    path = (settings or get_settings()).data_path / deck.stored_path
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise DomainError("deck_missing", f"The deck file '{deck.filename}' is missing from disk - upload it again.", 409) from exc
    if hashlib.sha256(data).hexdigest() != deck.sha256:
        raise DomainError("deck_corrupt", f"The deck file '{deck.filename}' changed on disk - upload it again.", 409)
    return data
