"""Enquiry categories (feedback round 2, E3): ``relevant_prospect`` (the default), ``channel_partner``, ``competitor``.

Haiku SUGGESTS one per post during the ordinary triage call (``enquiries.ai_category``, no extra call); the team confirms
or changes it on the Inbox card and later on the prospect page (``enquiries.category``). The category does the work:

* ``channel_partner``: accepting it ticks the existing "Partner" characteristic (system key ``partner``) on the prospect;
* ``competitor``: the company is filed into the Competitors registry (``file_as_competitor``) instead of the prospect
  pipeline; the enquiry is kept as evidence (linked to the competitor, discarded with a reason, searchable).

Assumed: the categories are a code enum (``ENQUIRY_CATEGORIES``), not a registry table - adding a value is a one-line
change + a prompt line, and nothing needs a table rebuild (no CHECK constraint on the column).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import Characteristic, Competitor, Enquiry
from sales0.db.models.enums_crm import DEFAULT_ENQUIRY_CATEGORY, ENQUIRY_CATEGORIES, ENQUIRY_CATEGORY_LABELS
from sales0.errors import DomainError
from sales0.services._common import clip, not_found, now_or, record_history
from sales0.services.crm import _keys, timeline
from sales0.services.crm.actor import Actor, actor_for

PARTNER_KEY = "partner"


def label(category: str | None) -> str:
    return ENQUIRY_CATEGORY_LABELS.get(category or DEFAULT_ENQUIRY_CATEGORY, "Relevant prospect")


def check(value: str | None) -> str:
    text = (value or "").strip().lower().replace(" ", "_")
    if text not in ENQUIRY_CATEGORIES:
        raise DomainError("category_invalid", "Choose Relevant prospect, Channel partner or Competitor.", 400)
    return text


def partner_characteristic_id(db: Session) -> int | None:
    return db.scalar(select(Characteristic.id).where(Characteristic.system_key == PARTNER_KEY))


def tick_partner(db: Session, prospect_id: int, *, actor: Actor, now: datetime | None = None) -> bool:
    """Add the "Partner" characteristic to the prospect (idempotent). False when the registry has no such row."""
    from sales0.services.crm import characteristics

    cid = partner_characteristic_id(db)
    if cid is None:
        return False
    characteristics.add_to(db, "prospect", prospect_id, [cid], actor=actor, now=now)
    return True


def set_category(
    db: Session, enquiry_id: int, value: str, *, actor_user_id: int | None = None, acted_by_member_id: int | None = None,
    now: datetime | None = None,
) -> Enquiry:
    """The team's call on the category. Records history + a timeline note; ``channel_partner`` on an enquiry that already
    has a prospect ticks Partner on it. ``competitor`` only labels (use ``file_as_competitor`` to file it)."""
    new = check(value)
    e = db.get(Enquiry, enquiry_id)
    if e is None:
        raise not_found()
    if e.category == new:
        return e
    stamp = now_or(now)
    actor = actor_for(db, actor_user_id, acted_by_member_id)
    record_history(db, "enquiry", e.id, "category", e.category, new, actor_user_id=actor_user_id, now=stamp)
    before, e.category = e.category, new
    timeline.add_event(
        db, subject_type="enquiry", subject_id=e.id, kind="note", actor=actor, occurred_at=stamp,
        body=f"Category: {label(before)} -> {label(new)}", data={"system": True, "category": {"from": before, "to": new}},
    )
    db.flush()
    if new == "channel_partner" and e.prospect_id:
        tick_partner(db, e.prospect_id, actor=actor, now=stamp)
    return e


def competitor_name_for(e: Enquiry) -> str:
    return clip(_keys.collapse(e.company or e.author_name or e.contact_person or ""), 200) or ""


def file_as_competitor(
    db: Session, enquiry_id: int, *, name: str | None = None, website: str | None = None, actor_user_id: int | None = None,
    acted_by_member_id: int | None = None, now: datetime | None = None,
) -> Competitor:
    """File the enquiry's company into the Competitors registry instead of the prospect pipeline.

    Reuses a competitor with the same name (or website), else creates one; links the enquiry as evidence; sets the category;
    a still-unreviewed enquiry is discarded (reason "Filed as competitor: <name>"), an accepted one whose prospect is still
    untouched (stage New, never contacted) has that prospect closed (reason ``competitor``).
    """
    from sales0.services import inbox
    from sales0.services.crm import prospects, registries

    e = db.get(Enquiry, enquiry_id)
    if e is None:
        raise not_found()
    nm = clip(_keys.collapse(name), 200) or competitor_name_for(e)
    if not nm:
        raise DomainError("competitor_invalid", "Give the competitor's name - the post names no company.", 400)
    stamp = now_or(now)
    actor = actor_for(db, actor_user_id, acted_by_member_id)
    row = db.scalar(select(Competitor).where(Competitor.name_key == _keys.name_key(nm)))
    if row is None and website:
        key = _keys.domain_key(website)
        if key:
            row = db.scalar(select(Competitor).where(Competitor.website_key == key))
    created = row is None
    if row is None:
        view = registries.create_competitor(db, name=nm, website=website or None, notes=f"Filed from enquiry #{e.id}", actor=actor, now=stamp)
        row = db.get(Competitor, view.id)
    assert row is not None
    registries.link_competitor(db, row.id, "enquiry", e.id, actor=actor, now=stamp)
    set_category(db, e.id, "competitor", actor_user_id=actor_user_id, acted_by_member_id=acted_by_member_id, now=stamp)
    timeline.add_event(
        db, subject_type="enquiry", subject_id=e.id, kind="note", actor=actor, occurred_at=stamp,
        body=f"Filed in Competitors: {row.name}" + (" (new)" if created else ""),
        data={"system": True, "competitor_id": row.id, "created": created},
    )
    if e.review_status == "pending_review":
        inbox.decide(db, [e.id], "discard", f"Filed as competitor: {row.name}"[:200], actor_user_id, now=stamp,
                     actor_member_id=actor_user_id, acted_by_member_id=acted_by_member_id)
    elif e.prospect_id:
        p = prospects.get_prospect(db, e.prospect_id)
        if p.status == "open" and p.stage == "new" and p.contacted_at is None:
            prospects.close(db, p.id, "competitor", actor=actor, now=stamp)
    db.flush()
    return row


__all__ = ["PARTNER_KEY", "check", "competitor_name_for", "file_as_competitor", "label", "partner_characteristic_id", "set_category", "tick_partner"]
