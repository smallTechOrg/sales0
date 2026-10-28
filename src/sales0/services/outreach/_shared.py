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


ACTED_BY_MARK = " | by_member_id="
_ACTED_BY_RE = re.compile(r"\s*\| by_member_id=(\d+)$")


def acted_by_of(member: object | None) -> int | None:
    """Id of the real admin when ``member`` is being acted as by someone else (transient ``acted_by``), else None."""
    by = getattr(member, "acted_by", None) if member is not None else None
    mid = getattr(member, "id", None)
    return by.id if by is not None and by.id != mid else None


def split_acted_by(detail: str | None) -> tuple[str | None, int | None]:
    """(detail without the audit suffix, acting admin id or None)."""
    if not detail:
        return detail, None
    m = _ACTED_BY_RE.search(detail)
    return (detail[: m.start()] or None, int(m.group(1))) if m else (detail, None)


def add_event(
    db: Session,
    message_id: int,
    kind: str,
    member_id: int | None,
    detail: str | None = None,
    now: datetime | None = None,
    *,
    acted_by_id: int | None = None,
) -> OutreachEvent:
    """Append one audit row (never updated or deleted). ``detail`` must never contain secrets.
    ``acted_by_id`` (an admin acting as ``member_id``) is stored as a detail suffix -> timeline "X as Y"."""
    text = detail[:900] if detail else None
    if acted_by_id is not None:
        text = (text or "") + f"{ACTED_BY_MARK}{acted_by_id}"
    row = OutreachEvent(
        message_id=message_id, kind=kind, member_id=member_id,
        detail=text, created_at=now_or(now),
    )  # fmt: skip
    db.add(row)
    return row
