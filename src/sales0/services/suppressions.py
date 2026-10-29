"""The suppression list: addresses sales0 must never draft to or send to."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import Suppression
from sales0.errors import DomainError
from sales0.services._common import clip, now_or
from sales0.services.outreach._shared import split_addrs


_GMAIL = ("gmail.com", "googlemail.com")


def _norm(address: str) -> str:
    return address.strip().strip("<>").lower()


def canonical(address: str) -> str:
    """Canonical form used to compare addresses: lowercase, ``+tag`` stripped from the local part,
    gmail/googlemail dots removed and googlemail.com -> gmail.com. ``@domain`` / ``*@domain`` (a
    domain-level entry) canonicalises to ``@domain``. Anything without ``@`` is returned lowercased."""
    a = _norm(address)
    if "@" not in a:
        return a
    local, _, domain = a.rpartition("@")
    domain = domain.strip().rstrip(".")
    local = local.strip()
    if local in ("", "*"):
        return "@" + domain
    local = local.split("+", 1)[0]
    if domain in _GMAIL:
        domain = "gmail.com"
        local = local.replace(".", "")
    return f"{local}@{domain}"


def _domain_of(canon: str) -> str:
    return canon.rpartition("@")[2]


def add(
    db: Session, address: str, reason: str, *, source_reply_id: int | None = None, note: str | None = None,
    now: datetime | None = None,
) -> Suppression:
    """Idempotent: an already-suppressed address keeps its first row."""
    addr = canonical(address)
    if not addr or "@" not in addr or addr == "@" or _domain_of(addr) == "":
        raise DomainError("invalid_address", "Enter a full email address (or @domain.com to block a whole domain).", 400)
    for existing in db.execute(select(Suppression)).scalars():
        if canonical(existing.address) == addr:
            return existing
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
    """The subset of the given addresses (strings or comma lists) that are suppressed, lowercased.

    Matching is on the canonical form (plus-tags, gmail dots) and on domain-level entries
    (``@example.com``); stored rows are canonicalised at compare time so no migration is needed."""
    wanted: list[str] = []
    for a in addrs:
        wanted += [_norm(x) for x in split_addrs(a)]
    if not wanted:
        return []
    stored = {canonical(x) for x in db.execute(select(Suppression.address)).scalars()}
    domains = {x for x in stored if x.startswith("@")}
    out: list[str] = []
    for w in dict.fromkeys(wanted):
        c = canonical(w)
        if c in stored or (c.startswith("@") is False and "@" in c and "@" + _domain_of(c) in domains):
            out.append(w)
    return out


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
