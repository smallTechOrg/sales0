"""Keyword CRUD (capability search-setup)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.models import AppMeta, Keyword
from sales0.errors import DomainError
from sales0.logging import get_logger
from sales0.services._common import clip, not_found
from sales0.services.keyword_library import SUGGESTED_KEYWORDS
from sales0.services.views import KeywordView

log = get_logger("sales0.services.keywords")

SEEDED_META_KEY = "keywords_seeded_v1"


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()

MIN_LEN = 2
MAX_LEN = 100
_WS = re.compile(r"\s+")


def normalize_keyword(text: str | None) -> str:
    """Trim and collapse inner whitespace; original case is kept."""
    return _WS.sub(" ", text or "").strip()


def _view(row: Keyword) -> KeywordView:
    return KeywordView(
        id=row.id,
        text=row.text,
        active=row.active,
        last_run_at=row.last_run_at,
        created_at=row.created_at,
        group_name=row.group_name,
        suggested=row.suggested_key is not None,
        scanned_total=row.scanned_total or 0,
        relevant_total=row.relevant_total or 0,
        last_scanned=row.last_scanned or 0,
        last_relevant=row.last_relevant or 0,
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


def set_keyword_active(db: Session, keyword_id: int, active: bool, actor_user_id: int | None = None) -> KeywordView:
    row = db.get(Keyword, keyword_id)
    if row is None:
        raise not_found()
    row.active = bool(active)
    if actor_user_id is not None:
        row.actor_user_id = actor_user_id  # last member to touch it
    db.flush()
    return _view(row)


def _validated(text: str | None) -> str:
    clean = normalize_keyword(text)
    if not (MIN_LEN <= len(clean) <= MAX_LEN):
        raise DomainError("keyword_invalid", "Keyword must be 2–100 characters.", 400)
    return clean


def update_keyword(
    db: Session, keyword_id: int, text: str | None = None, group_name: str | None | _Unset = UNSET,
    actor_user_id: int | None = None,
) -> KeywordView:
    """Edit text and/or group in place. ``text=None`` keeps the text; ``group_name`` omitted keeps the
    group, ``None``/blank clears it. A seeded keyword keeps its ``suggested_key`` whatever the edit."""
    row = db.get(Keyword, keyword_id)
    if row is None:
        raise not_found()
    if text is not None:
        clean = _validated(text)
        other = db.scalar(select(Keyword).where(Keyword.text_key == clean.lower(), Keyword.id != keyword_id))
        if other is not None:
            raise DomainError("keyword_duplicate", f"'{other.text}' is already in your keywords.", 409)
        row.text = clean
    if group_name is not UNSET:
        row.group_name = clip(group_name, 40) if isinstance(group_name, str) else None
    if actor_user_id is not None:
        row.actor_user_id = actor_user_id  # last member to touch it
    db.flush()
    return _view(row)


def record_yield(db: Session, keyword_id: int | None, scanned: int, relevant: int) -> None:
    """Add a finished scan to the keyword's yield stats (Core UPDATE; a deleted keyword updates 0 rows)."""
    if keyword_id is None:
        return
    db.execute(
        update(Keyword)
        .where(Keyword.id == keyword_id)
        .values(
            scanned_total=Keyword.scanned_total + int(scanned),
            relevant_total=Keyword.relevant_total + int(relevant),
            last_scanned=int(scanned),
            last_relevant=int(relevant),
        )
    )
    db.flush()
    db.expire_all()


@dataclass(frozen=True)
class SeedResult:
    added: int
    already_present: int


def seed_suggested(db: Session) -> SeedResult:
    """Insert each suggested phrase that has no row by ``suggested_key`` and no row by text. Never
    updates, re-activates, re-groups or re-texts anything."""
    keys = {k for k in db.scalars(select(Keyword.suggested_key)).all() if k}
    texts = set(db.scalars(select(Keyword.text_key)).all())
    added = 0
    for item in SUGGESTED_KEYWORDS:
        if item.key in keys or item.text.lower() in texts:
            continue
        db.add(Keyword(text=item.text, active=True, group_name=item.group, suggested_key=item.key))
        keys.add(item.key)
        texts.add(item.text.lower())
        added += 1
    db.flush()
    return SeedResult(added=added, already_present=len(SUGGESTED_KEYWORDS) - added)


def seed_suggested_once(db: Session) -> SeedResult | None:
    """Startup seeding: once ever (``app_meta.keywords_seeded_v1``) and only when enabled."""
    if not get_settings().seed_suggested_keywords:
        return None
    if db.get(AppMeta, SEEDED_META_KEY) is not None:
        return None
    result = seed_suggested(db)
    db.add(AppMeta(key=SEEDED_META_KEY, value="1"))
    db.flush()
    log.info("keywords_seeded", added=result.added, already_present=result.already_present)
    return result


__all__ = [
    "UNSET",
    "SeedResult",
    "add_keyword",
    "list_keywords",
    "normalize_keyword",
    "record_yield",
    "remove_keyword",
    "seed_suggested",
    "seed_suggested_once",
    "set_keyword_active",
    "update_keyword",
]
