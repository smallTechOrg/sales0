"""Recipient choices for Compose: the prospect's (and its company's) contacts with every e-mail they have.

One option per (contact, address): the prospect's primary contact first, then its other linked contacts, then the
company's remaining contacts; within a contact its primary address first. Merged-away contacts are skipped.
Suppressed addresses are listed but marked so the page shows them disabled (nothing can be drafted to them).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from sales0.db.models import Contact, ContactEmail, ContactLink, Prospect
from sales0.services import suppressions
from sales0.services.crm import _keys

MAX_OPTIONS = 50


@dataclass(frozen=True)
class RecipientOption:
    contact_id: int
    name: str
    email: str
    primary: bool  # the prospect's primary contact
    suppressed: bool
    source: str  # "prospect" | "company" | "search"

    @property
    def label(self) -> str:
        who = self.name or self.email
        tail = " (primary)" if self.primary else ""
        return f"{who} - {self.email}{tail}" + (" - suppressed" if self.suppressed else "")


def _emails_of(db: Session, contacts: list[Contact]) -> dict[int, list[str]]:
    """Each contact's addresses, its primary (``contacts.email`` / ``is_primary``) first, de-duplicated."""
    out: dict[int, list[str]] = {c.id: [c.email] if c.email else [] for c in contacts}
    if not contacts:
        return out
    rows = db.execute(
        select(ContactEmail.contact_id, ContactEmail.email)
        .where(ContactEmail.contact_id.in_([c.id for c in contacts]))
        .order_by(ContactEmail.contact_id, ContactEmail.is_primary.desc(), ContactEmail.id)
    ).all()
    for cid, email in rows:
        out[cid].append(email)
    return {cid: list(dict.fromkeys(e.strip().lower() for e in emails if e and e.strip())) for cid, emails in out.items()}


def _options(db: Session, ordered: list[tuple[Contact, str, bool]]) -> list[RecipientOption]:
    emails = _emails_of(db, [c for c, _, _ in ordered])
    flat = [e for es in emails.values() for e in es]
    blocked = set(suppressions.suppressed_among(db, flat)) if flat else set()
    out: list[RecipientOption] = []
    seen: set[str] = set()
    for contact, source, primary in ordered:
        for email in emails.get(contact.id, []):
            if email in seen:
                continue
            seen.add(email)
            out.append(RecipientOption(contact.id, (contact.full_name or "").strip(), email, primary, email in blocked, source))
    return out[:MAX_OPTIONS]


def for_prospect(db: Session, prospect_id: int | None, company_id: int | None = None) -> list[RecipientOption]:
    """Options for a prospect (its contacts, then its company's). ``company_id`` alone lists that company's contacts."""
    p = db.get(Prospect, prospect_id) if prospect_id is not None else None
    company_id = company_id or (p.company_id if p is not None else None)
    ordered: list[tuple[Contact, str, bool]] = []
    taken: set[int] = set()

    def add(rows: list[Contact], source: str) -> None:
        for c in rows:
            if c.id in taken or c.merged_into_id is not None:
                continue
            taken.add(c.id)
            ordered.append((c, source, p is not None and c.id == p.contact_id))

    if p is not None:
        linked = list(db.scalars(
            select(Contact).where(or_(
                Contact.id == p.contact_id,
                Contact.id.in_(select(ContactLink.contact_id).where(ContactLink.entity_type == "prospect", ContactLink.entity_id == p.id)),
            ))
        ))  # fmt: skip
        linked.sort(key=lambda c: (c.id != p.contact_id, (c.full_name or c.email or "").lower(), c.id))
        add(linked, "prospect")
    if company_id:
        add(list(db.scalars(select(Contact).where(Contact.company_id == company_id).order_by(
            func.lower(func.coalesce(Contact.full_name, Contact.email, "")), Contact.id
        ))), "company")  # fmt: skip
    return _options(db, ordered)


def search(db: Session, q: str, *, limit: int = 20) -> list[RecipientOption]:
    """Contacts matching ``q`` (name / e-mail / company words) that have at least one address."""
    words = [w for w in _keys.name_key(q or "").split(" ") if w]
    if not words:
        return []
    stmt = select(Contact).where(Contact.merged_into_id.is_(None))
    for w in words:
        stmt = stmt.where(Contact.search_text.like(f"%{_keys.like_escape(w)}%", escape="\\"))
    rows = list(db.scalars(stmt.order_by(func.lower(func.coalesce(Contact.full_name, Contact.email, "")), Contact.id).limit(limit)))
    return _options(db, [(c, "search", False) for c in rows])[:limit]


__all__ = ["RecipientOption", "for_prospect", "search"]
