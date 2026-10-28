"""Leads: opened only after contact AND a response; Meeting scheduled -> Proposal sent -> Won / Lost / No response."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from sales0.db.models import Client, Company, Enquiry, Lead, OutreachMessage, Prospect, TimelineEvent
from sales0.db.models.enums_crm import LEAD_STAGES
from sales0.errors import DomainError
from sales0.services._common import clip, now_or, not_found
from sales0.services.crm import _keys, clients, contacts, flags, followups, timeline
from sales0.services.crm._common import invalid, page_params, today_local
from sales0.services.crm.actor import TEAM, Actor
from sales0.services.crm.stages import TERMINAL_LEAD_STAGES, stage_label
from sales0.services.crm.views import LeadDetail, LeadPage, LeadView

OPEN_START_STAGES = ("meeting_scheduled", "proposal_sent")


def views_for(db: Session, rows: Iterable[Lead], *, now: datetime | None = None) -> list[LeadView]:
    rows = list(rows)
    if not rows:
        return []
    pids = {r.prospect_id for r in rows}
    prospects = {p.id: p for p in db.scalars(select(Prospect).where(Prospect.id.in_(pids)))}
    cids = {p.company_id for p in prospects.values() if p.company_id} | {r.company_id for r in rows if r.company_id}
    company_names = dict(db.execute(select(Company.id, Company.name).where(Company.id.in_(cids))).all()) if cids else {}
    flag_map = flags.flags_for(db, "prospect", list(pids))
    last = dict(
        db.execute(
            select(TimelineEvent.prospect_id, func.max(TimelineEvent.occurred_at)).where(TimelineEvent.prospect_id.in_(pids)).group_by(TimelineEvent.prospect_id)
        ).all()
    )
    stamp = now_or(now)
    out: list[LeadView] = []
    for r in rows:
        p = prospects[r.prospect_id]
        seen = last.get(r.prospect_id)
        out.append(
            LeadView(
                id=r.id, prospect_id=r.prospect_id, prospect_name=p.name,
                company_name=company_names.get(r.company_id or p.company_id), stage=r.stage, stage_label=stage_label(r.stage),
                opened_at=r.opened_at, closed_at=r.closed_at, meeting_at=r.meeting_at, proposal_sent_on=r.proposal_sent_on,
                value_estimate=r.value_estimate, outcome_note=r.outcome_note, flags=flag_map.get(r.prospect_id, []),
                days_since_activity=max((stamp - seen).days, 0) if seen is not None else None,
            )
        )
    return out


def _get(db: Session, lead_id: int) -> Lead:
    row = db.get(Lead, lead_id)
    if row is None:
        raise not_found()
    return row


def _set_proposal_date(db: Session, ld: Lead, p: Prospect, when: date) -> None:
    ld.proposal_sent_on = when
    e = db.get(Enquiry, p.origin_enquiry_id) if p.origin_enquiry_id else None
    if e is None:
        e = db.scalar(select(Enquiry).where(Enquiry.prospect_id == p.id).order_by(Enquiry.date_added, Enquiry.id).limit(1))
    if e is not None and e.date_proposal_sent != when:
        e.date_proposal_sent = when


def convert(
    db: Session, prospect_id: int, *, stage: str = "meeting_scheduled", meeting_at: datetime | None = None,
    opened_by: str = "manual", actor: Actor = TEAM, note: str | None = None, now: datetime | None = None,
) -> LeadView:
    p = db.get(Prospect, prospect_id)
    if p is None:
        raise not_found()
    if stage not in OPEN_START_STAGES:
        raise invalid("stage_invalid", "Choose a valid stage.")
    if opened_by not in ("reply", "manual", "sheet"):
        opened_by = "manual"
    if db.scalar(select(Lead.id).where(Lead.prospect_id == p.id, Lead.closed_at.is_(None))) is not None:
        raise DomainError("lead_exists", "This prospect already has an open lead.", 409)
    if p.status == "closed":
        raise DomainError("prospect_closed", "This prospect is closed - reopen it first.", 409)
    if p.contacted_at is None:
        raise DomainError("lead_needs_contact", "This prospect has not been contacted yet - mark it Contacted first.", 409)
    if p.responded_at is None:
        raise DomainError("lead_needs_response", "No response is recorded yet - press 'Response received' first.", 409)
    stamp = now_or(now)
    ld = Lead(
        prospect_id=p.id, company_id=p.company_id, stage=stage, stage_source="app", opened_at=stamp, meeting_at=meeting_at,
        opened_by=opened_by, actor=actor.label[:80], actor_member_id=actor.member_id, created_at=stamp, updated_at=stamp,
    )
    db.add(ld)
    db.flush()
    p.status, p.updated_at = "converted", stamp
    p.actor, p.actor_member_id = actor.label[:80], actor.member_id
    db.flush()
    if stage == "proposal_sent":
        _set_proposal_date(db, ld, p, today_local(stamp))
    followups.cancel_open(db, prospect_id=p.id, reason="lead_created", actor=actor, now=stamp)
    if p.contact_id:
        contacts.link(db, p.contact_id, "lead", ld.id, "buyer")
    timeline.add_event(
        db, subject_type="lead", subject_id=ld.id, kind="lead_created",
        body=f"Lead created ({stage_label(stage)})" + (f" - {note.strip()}" if note and note.strip() else ""),
        data={"stage": stage, "opened_by": opened_by, **({"note": note.strip()} if note and note.strip() else {})},
        actor=actor, occurred_at=stamp,
    )
    return views_for(db, [ld], now=stamp)[0]


def move_stage(
    db: Session, lead_id: int, stage: str, *, proposal_sent_on: date | None = None, meeting_at: datetime | None = None,
    value_estimate: int | None = None, outcome_note: str | None = None, actor: Actor = TEAM, now: datetime | None = None,
) -> LeadView:
    if stage not in LEAD_STAGES:
        raise invalid("stage_invalid", "Choose a valid stage.")
    ld = _get(db, lead_id)
    p = db.get(Prospect, ld.prospect_id)
    assert p is not None
    if value_estimate is not None and value_estimate < 0:
        raise invalid("field_invalid", "The value estimate must be zero or more.")
    stamp = now_or(now)
    prev = ld.stage
    was_closed = ld.closed_at is not None
    if was_closed and stage not in TERMINAL_LEAD_STAGES:
        if db.scalar(select(Lead.id).where(Lead.prospect_id == p.id, Lead.closed_at.is_(None), Lead.id != ld.id)) is not None:
            raise DomainError("lead_exists", "This prospect already has an open lead.", 409)
    edited: list[str] = []
    if meeting_at is not None and meeting_at != ld.meeting_at:
        ld.meeting_at = meeting_at
        edited.append("meeting_at")
    if value_estimate is not None and value_estimate != ld.value_estimate:
        ld.value_estimate = value_estimate
        edited.append("value_estimate")
    note = clip(outcome_note, 500)
    if note is not None and note != ld.outcome_note:
        ld.outcome_note = note
        edited.append("outcome_note")
    if stage == prev and not edited and not (stage == "proposal_sent" and proposal_sent_on and proposal_sent_on != ld.proposal_sent_on):
        return views_for(db, [ld], now=stamp)[0]
    ld.stage, ld.stage_source = stage, "app"
    ld.actor, ld.actor_member_id, ld.updated_at = actor.label[:80], actor.member_id, stamp
    if stage in TERMINAL_LEAD_STAGES:
        if ld.closed_at is None:
            ld.closed_at = stamp
    else:
        ld.closed_at = None
    if stage == "proposal_sent":
        _set_proposal_date(db, ld, p, proposal_sent_on or ld.proposal_sent_on or today_local(stamp))
    elif proposal_sent_on is not None:
        _set_proposal_date(db, ld, p, proposal_sent_on)
    p.updated_at = stamp
    db.flush()
    extra: dict = {}
    if stage in ("lost", "no_response") and p.linkedin_profile_id:
        from sales0.services.crm import prospects

        if prospects._set_profile_tracked(db, p, False):  # noqa: SLF001
            extra["tracked"] = False
    if stage in TERMINAL_LEAD_STAGES:
        followups.cancel_open(db, lead_id=ld.id, reason=f"lead_{stage}", actor=actor, now=stamp)
    if stage == "won":
        clients.create_from_lead(db, ld.id, actor=actor, now=stamp)
    if prev != stage:
        kind = "proposal_sent" if stage == "proposal_sent" else "lead_stage"
        body = (
            f"Proposal sent on {ld.proposal_sent_on.isoformat()}" if stage == "proposal_sent" and ld.proposal_sent_on
            else f"Lead stage: {stage_label(prev)} -> {stage_label(stage)}"
        )
        timeline.add_event(
            db, subject_type="lead", subject_id=ld.id, kind=kind, body=body,
            data={"from": prev, "to": stage, **extra, **({"proposal_sent_on": ld.proposal_sent_on.isoformat()} if ld.proposal_sent_on and stage == "proposal_sent" else {})},
            actor=actor, occurred_at=stamp,
        )
    elif edited:
        timeline.add_event(db, subject_type="lead", subject_id=ld.id, kind="note", body="Lead details edited: " + ", ".join(edited),
                           data={"system": True, "fields": edited}, actor=actor, occurred_at=stamp)
    return views_for(db, [ld], now=stamp)[0]


def get_lead(db: Session, lead_id: int) -> LeadDetail:
    from sales0.services.crm import prospects

    ld = _get(db, lead_id)
    base = views_for(db, [ld])[0]
    c = db.scalar(select(Client).where(Client.lead_id == ld.id))
    enq_rows = list(db.scalars(select(Enquiry).where(Enquiry.prospect_id == ld.prospect_id).order_by(Enquiry.date_added.desc(), Enquiry.id.desc())))
    from sales0.db.models import TeamMember
    from sales0.services.crm.views import LinkedMessageView

    conds = [OutreachMessage.prospect_id == ld.prospect_id]
    if enq_rows:
        conds.append(OutreachMessage.enquiry_id.in_([e.id for e in enq_rows]))
    msgs = db.execute(
        select(OutreachMessage, TeamMember.name).join(TeamMember, TeamMember.id == OutreachMessage.member_id).where(or_(*conds))
        .order_by(OutreachMessage.created_at.desc(), OutreachMessage.id.desc())
    ).all()
    return LeadDetail(
        **base.model_dump(),
        timeline=timeline.list_timeline(db, prospect_id=ld.prospect_id),
        follow_ups=followups.for_lead(db, ld.id),
        client=clients.view_of(c) if c else None,
        enquiries=[prospects.linked_enquiry_view(e) for e in enq_rows],
        messages=[LinkedMessageView(id=m.id, subject=m.subject, state=m.state, sent_at=m.sent_at, reply_status=m.reply_status, owner_name=o) for m, o in msgs],
    )


def list_leads(
    db: Session, *, stage: str | None = None, open_only: bool = True, q: str | None = None, page: int = 1,
    page_size: int | None = None,
) -> LeadPage:
    page, size = page_params(page, page_size)
    stmt = select(Lead).join(Prospect, Prospect.id == Lead.prospect_id)
    if open_only:
        stmt = stmt.where(Lead.closed_at.is_(None))
    if stage:
        stmt = stmt.where(Lead.stage == stage)
    if q and q.strip():
        like = f"%{_keys.like_escape(_keys.name_key(q))}%"
        stmt = stmt.where(or_(func.lower(Prospect.name).like(like, escape="\\"),
                              Prospect.company_id.in_(select(Company.id).where(Company.name_key.like(like, escape="\\")))))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(db.scalars(stmt.order_by(Lead.updated_at.desc(), Lead.id.desc()).limit(size).offset((page - 1) * size)))
    return LeadPage(items=views_for(db, rows), total=total, page=page, page_size=size)


__all__ = ["convert", "get_lead", "list_leads", "move_stage", "views_for"]
