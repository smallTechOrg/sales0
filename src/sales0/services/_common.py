"""Small helpers shared by the services (not part of the public contract)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from sales0.clock import ensure_utc, utcnow
from sales0.errors import DomainError

NOT_FOUND_MESSAGE = "That item no longer exists — refresh the page."


def not_found() -> DomainError:
    return DomainError("not_found", NOT_FOUND_MESSAGE, 404)


def now_or(now: datetime | None) -> datetime:
    return ensure_utc(now) if now is not None else utcnow()


def clip(value: str | None, limit: int) -> str | None:
    """Trim; empty -> None; longer than ``limit`` -> cut with an ellipsis."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 0)].rstrip() + "…"


def record_history(
    db: Session,
    entity_type: str,
    entity_id: int,
    field: str,
    from_value: str | None,
    to_value: str | None,
    *,
    note: str | None = None,
    actor_user_id: int | None = None,
    now: datetime | None = None,
) -> None:
    from sales0.db.models import StatusHistory

    db.add(
        StatusHistory(
            entity_type=entity_type,
            entity_id=entity_id,
            field=field,
            from_value=from_value,
            to_value=to_value,
            note=clip(note, 500) if note else None,
            changed_at=now_or(now),
            actor_user_id=actor_user_id,
        )
    )
