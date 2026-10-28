"""The suppression list: addresses sales0 must never draft to or send to."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import Suppression
from sales0.errors import DomainError
from sales0.services._common import clip, now_or
from sales0.services.outreach._shared import split_addrs


def _norm(address: str) -> str:
    return address.strip().strip("<>").lower()


def add(
    db: Session, address: str, reason: str, *, source_reply_id: int | None = None, note: str | None = None,
    now: datetime | None = None,
) -> Suppression:
    """Idempotent: an already-suppressed address keeps its first row."""
    addr = _norm(address)
    if not addr or "@" not in addr:
        raise DomainError("invalid_address", "Enter a full email address.", 400)
    row = db.execute(select(Suppression).where(Suppression.address == addr)).scalar_one_or_none()
    if row is not None:
        return row
    row = Suppression(address=addr[:254], reason=reason, source_reply_id=source_reply_id, note=clip(note, 300), created_at=now_or(now))
    db.add(row)
    db.flush()
    return row


def remove(db: Session, suppression_id: int) -> None:
    row = db.get(Suppression, suppression_id)
    if row is None:
        raise DomainError("not_found", "That suppression no longer exists.", 404)
    db.delete(row)
    db.flush()


def list_all(db: Session) -> list[Suppression]:
    return list(db.execute(select(Suppression).order_by(Suppression.created_at.desc(), Suppression.id.desc())).scalars())


def suppressed_among(db: Session, *addrs: str | list[str] | None) -> list[str]:
    """The subset of the given addresses (strings or comma lists) that are suppressed, lowercased."""
    wanted: list[str] = []
    for a in addrs:
        wanted += [_norm(x) for x in split_addrs(a)]
    if not wanted:
        return []
    hit = set(db.execute(select(Suppression.address).where(Suppression.address.in_(wanted))).scalars())
    return [a for a in dict.fromkeys(wanted) if a in hit]


def assert_not_suppressed(db: Session, *addrs: str | list[str] | None) -> None:
    """Raise DomainError('recipient_suppressed') when any address is on the list."""
    hit = suppressed_among(db, *addrs)
    if hit:
        raise DomainError(
            "recipient_suppressed",
            f"{', '.join(hit)} is on the suppression list (unsubscribed or bounced), so no email can be drafted or sent to it. "
            "Remove it on the Replies > Suppressions page only if that is a mistake.",
            409,
        )
