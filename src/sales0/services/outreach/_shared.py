"""Helpers shared by the outreach services: address lists and the append-only event trail."""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy.orm import Session

from sales0.db.models import OutreachEvent
from sales0.services._common import now_or

_SPLIT = re.compile(r"[,;\s]+")


def split_addrs(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """'a@x.com, b@y.com' (or a list) -> ['a@x.com', 'b@y.com']; blanks dropped, order kept, duplicates removed."""
    if value is None:
        return []
    parts = [value] if isinstance(value, str) else list(value)
    out: list[str] = []
    for part in parts:
        for token in _SPLIT.split(part.strip()):
            token = token.strip("<>")
            if token and token.lower() not in {a.lower() for a in out}:
                out.append(token)
    return out


def join_addrs(addrs: list[str]) -> str:
    return ", ".join(addrs)


def add_event(
    db: Session, message_id: int, kind: str, member_id: int | None, detail: str | None = None, now: datetime | None = None
) -> OutreachEvent:
    """Append one audit row (never updated or deleted). ``detail`` must never contain secrets."""
    row = OutreachEvent(
        message_id=message_id, kind=kind, member_id=member_id,
        detail=(detail[:1000] if detail else None), created_at=now_or(now),
    )  # fmt: skip
    db.add(row)
    return row
