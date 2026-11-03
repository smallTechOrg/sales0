"""Long team-editable text settings (``text_settings``), e.g. the drafting instructions every AI email draft follows."""

from __future__ import annotations

from sqlalchemy.orm import Session

from sales0.db.models import TextSetting
from sales0.errors import DomainError

DRAFTING_INSTRUCTIONS = "drafting_instructions"
MAX_CHARS = 4000


def get(db: Session, key: str) -> str:
    row = db.get(TextSetting, key)
    return row.value if row is not None else ""


def put(db: Session, key: str, value: str, *, actor: str | None = None) -> str:
    text = (value or "").replace("\r\n", "\n").strip()
    if len(text) > MAX_CHARS:
        raise DomainError("setting_invalid", f"Keep it under {MAX_CHARS} characters.", 400)
    row = db.get(TextSetting, key)
    if row is None:
        row = TextSetting(key=key, value=text, updated_by=(actor or "")[:80] or None)
        db.add(row)
    else:
        row.value, row.updated_by = text, (actor or "")[:80] or None
    db.flush()
    return text


__all__ = ["DRAFTING_INSTRUCTIONS", "MAX_CHARS", "get", "put"]
