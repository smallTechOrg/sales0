"""Suggestion inbox: list, accept (applies the change to keywords) and dismiss.

Decided suggestions are kept (their ``dedupe_key`` stops the agent from proposing them again).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.db.models import Keyword, Suggestion, TeamMember
from sales0.errors import DomainError
from sales0.services import keywords as keywords_service
from sales0.services._common import not_found, now_or


@dataclass(frozen=True)
class Decision:
    suggestion: Suggestion
    note: str | None = None


def list_suggestions(
    db: Session,
    status: str | tuple[str, ...] | None = None,
    *,
    limit: int | None = None,
) -> list[Suggestion]:
    q = select(Suggestion).order_by(
        func.coalesce(Suggestion.decided_at, Suggestion.created_at).desc(),
        Suggestion.id.desc(),
    )
    if isinstance(status, str):
        q = q.where(Suggestion.status == status)
    elif status:
        q = q.where(Suggestion.status.in_(status))
    if limit:
        q = q.limit(limit)
    return list(db.scalars(q))


def pending_count(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Suggestion)
            .where(Suggestion.status == "pending")
        )
        or 0
    )


def evidence_items(row: Suggestion) -> list[dict[str, Any]]:
    """Parsed evidence snippets of a new-keyword suggestion ([] when absent or malformed)."""
    try:
        data = json.loads(row.evidence_json or "[]")
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [
        d for d in data if isinstance(d, dict) and isinstance(d.get("snippet"), str)
    ]


def evidence_stats(row: Suggestion) -> dict[str, Any]:
    """Parsed numbers of a pause suggestion ({} when absent or malformed)."""
    try:
        data = json.loads(row.evidence_json or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _pending(db: Session, suggestion_id: int) -> Suggestion:
    row = db.get(Suggestion, suggestion_id)
    if row is None:
        raise not_found()
    if row.status != "pending":
        raise DomainError(
            "suggestion_decided", f"This suggestion was already {row.status}.", 409
        )
    return row


def _decide(
    row: Suggestion, status: str, member: TeamMember | None, now: datetime | None
) -> None:
    row.status = status
    row.decided_at = now_or(now)
    row.decided_by_member_id = member.id if member is not None else None


def accept(
    db: Session,
    suggestion_id: int,
    member: TeamMember | None = None,
    now: datetime | None = None,
) -> Decision:
    row = _pending(db, suggestion_id)
    note: str | None = None
    if row.kind == "new_keyword":
        try:
            keywords_service.add_keyword(db, row.text)
        except DomainError as exc:
            if exc.code != "keyword_duplicate":
                raise
            note = f"'{row.text}' was already one of your keywords."
    else:
        kw = db.get(Keyword, row.keyword_id) if row.keyword_id is not None else None
        if kw is None or kw.text.strip().lower() != row.text.strip().lower():
            raise DomainError(
                "suggestion_stale",
                f"The keyword '{row.text}' no longer exists, so there is nothing to pause. Dismiss this suggestion.",
                409,
            )
        keywords_service.set_keyword_active(db, kw.id, False)
    _decide(row, "accepted", member, now)
    db.flush()
    return Decision(row, note)


def dismiss(
    db: Session,
    suggestion_id: int,
    member: TeamMember | None = None,
    now: datetime | None = None,
) -> Decision:
    row = _pending(db, suggestion_id)
    _decide(row, "dismissed", member, now)
    db.flush()
    return Decision(row)


__all__ = [
    "Decision",
    "accept",
    "dismiss",
    "evidence_items",
    "evidence_stats",
    "list_suggestions",
    "pending_count",
]
