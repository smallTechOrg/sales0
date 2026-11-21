"""SMA-40: a person edits an enquiry's details by hand - the summary, company, contact, link to the post, date posted,
notes... (also the way to fill one in when the AI could not read it).

Only the fields given are changed. A new company name on an accepted enquiry attaches that company (the same as the
Inbox's "Company" box); a contact name / email on an accepted enquiry is kept as a person of the company."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from sales0.clock import local_today
from sales0.config import get_settings
from sales0.db.models import Enquiry, Prospect
from sales0.errors import DomainError
from sales0.services._common import not_found, now_or
from sales0.services.crm import timeline
from sales0.services.crm.actor import TEAM, Actor

# field -> (label, max length); dates and links are checked separately
TEXT_FIELDS: dict[str, tuple[str, int]] = {
    "requirement_summary": ("Summary", 1000),
    "company": ("Company", 200),
    "contact_person": ("Contact person", 200),
    "email": ("Email", 254),
    "phone": ("Phone", 50),
    "city": ("City", 100),
    "author_name": ("Posted by", 200),
    "source_url": ("Link to the post", 1000),
    "author_profile_url": ("Profile link", 1000),
    "notes": ("Notes", 4000),
}
LINK_FIELDS = ("source_url", "author_profile_url")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean(field: str, value: str | None) -> str | None:
    label, limit = TEXT_FIELDS[field]
    text = (value or "").strip() if field == "notes" else " ".join((value or "").split())
    if not text:
        return None
    if len(text) > limit:
        raise DomainError("enquiry_invalid", f"{label} is too long (max {limit} characters).", 400)
    if field in LINK_FIELDS and not text.lower().startswith(("http://", "https://")):
        raise DomainError("enquiry_invalid", f"{label} must start with http:// or https://.", 400)
    if field == "email" and not _EMAIL.match(text):
        raise DomainError("enquiry_invalid", "That email address does not look right.", 400)
    return text


def edit(
    db: Session, enquiry_id: int, *, fields: dict[str, str | None], date_posted: date | None = None,
    clear_date: bool = False, actor: Actor = TEAM, now: datetime | None = None,
) -> Enquiry:
    """Apply the given fields (flushed; the caller commits). Returns the enquiry."""
    e = db.get(Enquiry, enquiry_id)
    if e is None:
        raise not_found()
    stamp = now_or(now)
    unknown = set(fields) - set(TEXT_FIELDS)
    if unknown:
        raise DomainError("enquiry_invalid", f"Unknown field: {sorted(unknown)[0]}.", 400)
    clean = {k: _clean(k, v) for k, v in fields.items()}
    if date_posted is not None and date_posted > local_today(get_settings().tz, stamp):
        raise DomainError("enquiry_invalid", "The date posted cannot be in the future.", 400)
    changed: list[str] = []
    new_company = clean.pop("company", None) if "company" in clean else None
    company_given = "company" in fields
    for key, value in clean.items():
        if getattr(e, key) != value:
            setattr(e, key, value)
            changed.append(TEXT_FIELDS[key][0])
    new_date = date_posted if date_posted is not None else (None if clear_date else e.date_posted)
    if new_date != e.date_posted:
        e.date_posted = new_date
        changed.append("Date posted")
    if company_given and (new_company or "") != (e.company or ""):
        changed.append("Company")
        if new_company and e.review_status == "accepted":
            from sales0.services.crm import companies

            companies.attach_to_enquiry(db, e.id, name=new_company, actor=actor, now=stamp)
        else:
            e.company = new_company
    db.flush()
    if e.review_status == "accepted" and ("Contact person" in changed or "Email" in changed or "Phone" in changed):
        _keep_contact(db, e, actor=actor, now=stamp)
    if changed:
        timeline.add_event(
            db, subject_type="enquiry", subject_id=e.id, kind="note", actor=actor, occurred_at=stamp, enquiry_id=e.id,
            company_id=e.company_id, body=f"Enquiry edited: {', '.join(changed)}.", data={"system": True, "fields": changed},
        )  # fmt: skip
    db.flush()
    return e


def _keep_contact(db: Session, e: Enquiry, *, actor: Actor, now: datetime) -> None:
    if not (e.contact_person or e.email):
        return
    p = db.get(Prospect, e.prospect_id) if e.prospect_id else None
    if p is None:
        return
    from sales0.services.crm import prospects

    row = prospects._contact_for(  # noqa: SLF001 - the same "buyer" link an accept makes
        db, p, ("enquiry", e.id), "buyer", full_name=e.contact_person, email=e.email, phone=e.phone,
        company_id=e.company_id, origin="enquiry", actor=actor, now=now,
    )  # fmt: skip
    if row is not None:
        e.contact_id = e.contact_id or row.id
        if p.contact_id is None:
            p.contact_id = row.id


def form_fields(e: Any) -> dict[str, Any]:
    """The current values for the edit form."""
    return {k: getattr(e, k, None) or "" for k in TEXT_FIELDS}


__all__ = ["TEXT_FIELDS", "edit", "form_fields"]
