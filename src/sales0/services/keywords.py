"""Keyword CRUD (capability search-setup)."""

from __future__ import annotations

import re

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from sales0.db.models import Keyword
from sales0.errors import DomainError
from sales0.services._common import not_found
from sales0.services.views import KeywordView

MIN_LEN = 2
MAX_LEN = 100
_WS = re.compile(r"\s+")


def normalize_keyword(text: str | None) -> str:
    """Trim and collapse inner whitespace; original case is kept."""
    return _WS.sub(" ", text or "").strip()


def _view(row: Keyword) -> KeywordView:
    return KeywordView(
        id=row.id, text=row.text, active=row.active, last_run_at=row.last_run_at, created_at=row.created_at
    )


def list_keywords(db: Session) -> list[KeywordView]:
    rows = db.scalars(select(Keyword).order_by(Keyword.created_at, Keyword.id)).all()
    return [_view(r) for r in rows]


def add_keyword(db: Session, text: str, actor_user_id: int | None = None) -> KeywordView:
    clean = normalize_keyword(text)
    if not (MIN_LEN <= len(clean) <= MAX_LEN):
        raise DomainError("keyword_invalid", "Keyword must be 2–100 characters.", 400)
    existing = db.scalar(select(Keyword).where(Keyword.text_key == clean.lower()))
    if existing is not None:
        raise DomainError("keyword_duplicate", f"'{existing.text}' is already in your keywords.", 409)
    row = Keyword(text=clean, active=True, actor_user_id=actor_user_id)
    db.add(row)
    db.flush()
    return _view(row)


def remove_keyword(db: Session, keyword_id: int) -> None:
    """Delete the keyword. Run logs and keyword hits keep their text snapshot (FK SET NULL)."""
    if db.get(Keyword, keyword_id) is None:
        raise not_found()
    # Core delete so the database applies ON DELETE SET NULL to run-keyword rows and hits.
    db.execute(delete(Keyword).where(Keyword.id == keyword_id))
    db.flush()
    db.expire_all()


def set_keyword_active(db: Session, keyword_id: int, active: bool) -> KeywordView:
    row = db.get(Keyword, keyword_id)
    if row is None:
        raise not_found()
    row.active = bool(active)
    db.flush()
    return _view(row)


__all__ = ["add_keyword", "list_keywords", "normalize_keyword", "remove_keyword", "set_keyword_active"]
