"""The CRM timeline: append-only events, notes and the merged read model."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from sales0.db.models import Client, Enquiry, Lead, Prospect, StatusHistory, TimelineEvent, User
from sales0.db.models.enums_crm import TIMELINE_KINDS, TIMELINE_SUBJECTS
from sales0.errors import DomainError
from sales0.services._common import now_or, not_found
from sales0.services.crm.actor import SYSTEM, Actor
from sales0.services.crm.views import TimelineItemView

KIND_LABELS: dict[str, str] = {
    "created": "Created",
    "stage_change": "Stage changed",
    "note": "Note",
    "email_sent": "Email sent",
    "reply_received": "Reply received",
    "lead_created": "Lead created",
    "lead_stage": "Lead stage changed",
    "client_created": "Client created",
    "proposal_sent": "Proposal sent",
    "enquiry_linked": "Enquiry linked",
    "flag_set": "Flag set",
    "flag_cleared": "Flag cleared",
    "follow_up_added": "Follow-up added",
    "follow_up_done": "Follow-up done",
    "follow_up_cancelled": "Follow-up cancelled",
    "tracked_changed": "Tracking changed",
    "contact_merged": "Contacts merged",
    "contact_linked": "Contact linked",
    "call": "Call",
    "meeting": "Meeting",
    "research": "Research",
    "import": "Imported",
    "sheet_created": "Added from sheet",
    "sheet_updated": "Updated from sheet",
    "sheet_removed": "Removed from sheet",
    "sheet_restored": "Restored in sheet",
    "sheet_status": "Sheet status",
}
_SCOPES = ("prospect_id", "enquiry_id", "company_id", "contact_id")
_HREFS = {
    "prospect": "/pipeline/prospects/{}",
    "lead": "/pipeline/leads/{}",
    "client": "/pipeline/clients/{}",
    "contact": "/contacts/{}",
    "company": "/companies/{}",
    "competitor": "/competitors/competitor/{}",
}


def href_for(subject_type: str, subject_id: int) -> str | None:
    tpl = _HREFS.get(subject_type)
    return tpl.format(subject_id) if tpl else None


def derive_scopes(db: Session, subject_type: str, subject_id: int) -> dict[str, int | None]:
    """The aggregation scopes of a subject (a lead/client/email event also carries its prospect)."""
    out: dict[str, int | None] = dict.fromkeys(_SCOPES)
    if subject_type == "prospect":
        p = db.get(Prospect, subject_id)
        out.update(prospect_id=subject_id, company_id=p.company_id if p else None, contact_id=p.contact_id if p else None)
    elif subject_type == "lead":
        ld = db.get(Lead, subject_id)
        if ld is not None:
            p = db.get(Prospect, ld.prospect_id)
            out.update(prospect_id=ld.prospect_id, company_id=ld.company_id or (p.company_id if p else None))
    elif subject_type == "client":
        c = db.get(Client, subject_id)
        if c is not None:
            out.update(prospect_id=c.prospect_id, company_id=c.company_id)
    elif subject_type == "enquiry":
        e = db.get(Enquiry, subject_id)
        out.update(enquiry_id=subject_id, prospect_id=e.prospect_id if e else None, company_id=e.company_id if e else None,
                   contact_id=e.contact_id if e else None)
    elif subject_type == "contact":
        out.update(contact_id=subject_id)
    elif subject_type == "company":
        out.update(company_id=subject_id)
    return out


def add_event(
    db: Session,
    *,
    subject_type: str,
    subject_id: int,
    kind: str,
    body: str | None = None,
    data: dict[str, Any] | None = None,
    actor: Actor = SYSTEM,
    occurred_at: datetime | None = None,
    prospect_id: int | None = None,
    enquiry_id: int | None = None,
    company_id: int | None = None,
    contact_id: int | None = None,
) -> TimelineEvent:
    """Append one event. Scopes not given explicitly are derived from the subject."""
    if subject_type not in TIMELINE_SUBJECTS:
        raise ValueError(f"unknown timeline subject {subject_type!r}")
    if kind not in TIMELINE_KINDS:
        raise ValueError(f"unknown timeline kind {kind!r}")
    scopes = derive_scopes(db, subject_type, subject_id)
    explicit = {"prospect_id": prospect_id, "enquiry_id": enquiry_id, "company_id": company_id, "contact_id": contact_id}
    scopes.update({k: v for k, v in explicit.items() if v is not None})
    stamp = now_or(occurred_at)
    row = TimelineEvent(
        subject_type=subject_type,
        subject_id=subject_id,
        kind=kind,
        body=body,
        data=data,
        actor=actor.label[:80],
        actor_member_id=actor.member_id,
        occurred_at=stamp,
        created_at=stamp,
        **scopes,
    )
    db.add(row)
    db.flush()
    return row


def _item(row: TimelineEvent) -> TimelineItemView:
    return TimelineItemView(
        id=row.id,
        kind=row.kind,
        kind_label=KIND_LABELS.get(row.kind, row.kind),
        body=row.body,
        data=row.data,
        actor=row.actor,
        occurred_at=row.occurred_at,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        href=href_for(row.subject_type, row.subject_id),
    )


def add_note(
    db: Session, *, subject_type: str, subject_id: int, body: str, actor: Actor, now: datetime | None = None
) -> TimelineItemView:
    text = (body or "").strip()
    if not 1 <= len(text) <= 4000:
        raise DomainError("note_invalid", "A note is 1-4000 characters.", 400)
    if subject_type not in TIMELINE_SUBJECTS:
        raise ValueError(f"unknown timeline subject {subject_type!r}")
    _assert_subject_exists(db, subject_type, subject_id)
    row = add_event(db, subject_type=subject_type, subject_id=subject_id, kind="note", body=text, actor=actor, occurred_at=now)
    return _item(row)


def _assert_subject_exists(db: Session, subject_type: str, subject_id: int) -> None:
    from sales0.db.models import ChannelGroup, Company, Competitor, Contact

    model = {
        "prospect": Prospect, "lead": Lead, "client": Client, "contact": Contact, "company": Company,
        "enquiry": Enquiry, "competitor": Competitor, "group": ChannelGroup,
    }[subject_type]
    if db.get(model, subject_id) is None:
        raise not_found()


def _history_items(db: Session, enquiry_ids: list[int], before: datetime | None, limit: int) -> list[TimelineItemView]:
    if not enquiry_ids:
        return []
    stmt = (
        select(StatusHistory, User.display_name)
        .outerjoin(User, User.id == StatusHistory.actor_user_id)
        .where(StatusHistory.entity_type == "enquiry", StatusHistory.entity_id.in_(enquiry_ids))
        .order_by(StatusHistory.changed_at.desc(), StatusHistory.id.desc())
        .limit(limit)
    )
    if before is not None:
        stmt = stmt.where(StatusHistory.changed_at < before)
    out: list[TimelineItemView] = []
    for h, actor_name in db.execute(stmt).all():
        text = f"{h.field}: {h.from_value or '-'} -> {h.to_value or '-'}"
        out.append(
            TimelineItemView(
                id=-h.id,  # negative: a Phase 1 status_history row, not a timeline_events row
                kind="stage_change",
                kind_label="Status changed",
                body=text + (f" ({h.note})" if h.note else ""),
                data={"field": h.field, "from": h.from_value, "to": h.to_value, "source": "status_history"},
                actor=actor_name or "team",
                occurred_at=h.changed_at,
                subject_type="enquiry",
                subject_id=h.entity_id,
                href=None,
            )
        )
    return out


def list_timeline(
    db: Session,
    *,
    prospect_id: int | None = None,
    enquiry_id: int | None = None,
    company_id: int | None = None,
    contact_id: int | None = None,
    subject: tuple[str, int] | None = None,
    limit: int = 200,
    before: datetime | None = None,
) -> list[TimelineItemView]:
    given = [x for x in (prospect_id, enquiry_id, company_id, contact_id, subject) if x is not None]
    if len(given) != 1:
        raise ValueError("list_timeline needs exactly one scope argument")
    if prospect_id is not None:
        cond = TimelineEvent.prospect_id == prospect_id
        enq_where = Enquiry.prospect_id == prospect_id
    elif enquiry_id is not None:
        cond = TimelineEvent.enquiry_id == enquiry_id
        enq_where = Enquiry.id == enquiry_id
    elif company_id is not None:
        cond = TimelineEvent.company_id == company_id
        enq_where = Enquiry.company_id == company_id
    elif contact_id is not None:
        cond = TimelineEvent.contact_id == contact_id
        enq_where = Enquiry.contact_id == contact_id
    else:
        assert subject is not None
        cond = and_(TimelineEvent.subject_type == subject[0], TimelineEvent.subject_id == subject[1])
        enq_where = Enquiry.id == subject[1] if subject[0] == "enquiry" else None
    stmt = select(TimelineEvent).where(cond).order_by(TimelineEvent.occurred_at.desc(), TimelineEvent.id.desc()).limit(limit)
    if before is not None:
        stmt = stmt.where(TimelineEvent.occurred_at < before)
    items = [_item(r) for r in db.scalars(stmt)]
    if enq_where is not None:
        ids = list(db.scalars(select(Enquiry.id).where(enq_where)))
        items += _history_items(db, ids, before, limit)
    items.sort(key=lambda i: (i.occurred_at, i.id), reverse=True)
    return items[:limit]


def count_events(db: Session, **scope: int) -> int:
    from sqlalchemy import func

    col = {"prospect_id": TimelineEvent.prospect_id, "contact_id": TimelineEvent.contact_id,
           "company_id": TimelineEvent.company_id, "enquiry_id": TimelineEvent.enquiry_id}
    conds = [col[k] == v for k, v in scope.items()]
    return int(db.scalar(select(func.count()).select_from(TimelineEvent).where(or_(*conds) if len(conds) > 1 else conds[0])) or 0)


__all__ = ["KIND_LABELS", "add_event", "add_note", "derive_scopes", "href_for", "list_timeline"]
