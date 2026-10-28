"""Follow-up reminders: schedule on contact, due-today panel, complete / snooze / custom / cancel."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from sales0.clock import local_today
from sales0.config import get_settings
from sales0.db.models import Flag, FollowUp, Lead, Prospect
from sales0.errors import DomainError
from sales0.services._common import clip, now_or, not_found
from sales0.services.crm import timeline
from sales0.services.crm.actor import SYSTEM, TEAM, Actor
from sales0.services.crm.views import DuePanel, FollowUpView

KIND_LABELS = {
    "follow_up_1": "Follow-up 1",
    "follow_up_2": "Follow-up 2",
    "final_follow_up": "Final follow-up",
    "custom": "Follow-up",
}
_OPEN = (FollowUp.done_at.is_(None), FollowUp.cancelled_at.is_(None))


def _today(now: datetime | None = None) -> date:
    return local_today(get_settings().tz, now)


def _view(f: FollowUp, who: str, today: date) -> FollowUpView:
    return FollowUpView(
        id=f.id, prospect_id=f.prospect_id, lead_id=f.lead_id, who=who, kind=f.kind,
        kind_label=KIND_LABELS.get(f.kind, f.kind), due_on=f.due_on, note=f.note,
        overdue=(f.done_at is None and f.cancelled_at is None and f.due_on < today),
    )


def views_for(db: Session, rows: list[FollowUp], today: date | None = None) -> list[FollowUpView]:
    today = today or _today()
    pids = {r.prospect_id for r in rows if r.prospect_id}
    lids = {r.lead_id for r in rows if r.lead_id}
    names: dict[int, str] = dict(db.execute(select(Prospect.id, Prospect.name).where(Prospect.id.in_(pids))).all()) if pids else {}
    lead_names: dict[int, str] = (
        dict(db.execute(select(Lead.id, Prospect.name).join(Prospect, Prospect.id == Lead.prospect_id).where(Lead.id.in_(lids))).all())
        if lids else {}
    )
    return [_view(r, names.get(r.prospect_id, "") if r.prospect_id else lead_names.get(r.lead_id, ""), today) for r in rows]


def refresh_next(db: Session, prospect_id: int) -> None:
    """``prospects.next_follow_up_on`` = earliest open reminder of the prospect."""
    nxt = db.scalar(select(func.min(FollowUp.due_on)).where(FollowUp.prospect_id == prospect_id, *_OPEN))
    p = db.get(Prospect, prospect_id)
    if p is not None and p.next_follow_up_on != nxt:
        p.next_follow_up_on = nxt
        db.flush()


def _owner_ids(db: Session, f: FollowUp) -> tuple[int | None, int | None]:
    """(prospect_id, lead_id) with the prospect of a lead reminder resolved."""
    if f.prospect_id:
        return f.prospect_id, None
    ld = db.get(Lead, f.lead_id) if f.lead_id else None
    return (ld.prospect_id if ld else None), f.lead_id


def _event(db: Session, f: FollowUp, kind: str, body: str, actor: Actor, now: datetime | None, data: dict | None = None) -> None:
    prospect_id, lead_id = _owner_ids(db, f)
    subject = ("lead", lead_id) if lead_id else ("prospect", prospect_id or 0)
    timeline.add_event(
        db, subject_type=subject[0], subject_id=subject[1] or 0, kind=kind, body=body,
        data={"follow_up_id": f.id, "kind": f.kind, "due_on": f.due_on.isoformat(), **(data or {})},
        actor=actor, occurred_at=now, prospect_id=prospect_id,
    )


def _get(db: Session, follow_up_id: int) -> FollowUp:
    f = db.get(FollowUp, follow_up_id)
    if f is None:
        raise not_found()
    return f


def schedule_for_contact(db: Session, prospect_id: int, contacted_at: datetime, *, now: datetime | None = None) -> list[FollowUpView]:
    """The three reminders (calendar days from the contact date). No-op with ``no_follow_up`` or open auto reminders."""
    s = get_settings()
    p = db.get(Prospect, prospect_id)
    if p is None:
        raise not_found()
    if db.scalar(select(Flag.id).where(Flag.entity_type == "prospect", Flag.entity_id == p.id, Flag.kind == "no_follow_up", Flag.cleared_at.is_(None))):
        return []
    if db.scalar(select(FollowUp.id).where(FollowUp.prospect_id == p.id, FollowUp.auto.is_(True), *_OPEN).limit(1)):
        return []
    stamp = now_or(now)
    base = local_today(s.tz, contacted_at)
    plan = (("follow_up_1", s.followup_days_1), ("follow_up_2", s.followup_days_2), ("final_follow_up", s.followup_days_final))
    created: list[FollowUp] = []
    for kind, days in plan:
        f = FollowUp(prospect_id=p.id, kind=kind, due_on=base + timedelta(days=days), auto=True, created_at=stamp, updated_at=stamp)
        db.add(f)
        created.append(f)
    db.flush()
    refresh_next(db, p.id)
    timeline.add_event(
        db, subject_type="prospect", subject_id=p.id, kind="follow_up_added",
        body="Follow-ups scheduled: " + ", ".join(f"{KIND_LABELS[f.kind]} on {f.due_on.isoformat()}" for f in created),
        data={"follow_up_ids": [f.id for f in created], "due": [f.due_on.isoformat() for f in created]},
        actor=SYSTEM, occurred_at=stamp, prospect_id=p.id,
    )
    return views_for(db, created, _today(now))


def _due_query(today: date):  # type: ignore[no-untyped-def]
    no_follow = select(Flag.entity_id).where(Flag.entity_type == "prospect", Flag.kind == "no_follow_up", Flag.cleared_at.is_(None))
    return (
        select(FollowUp)
        .outerjoin(Lead, Lead.id == FollowUp.lead_id)
        .join(Prospect, Prospect.id == func.coalesce(FollowUp.prospect_id, Lead.prospect_id))
        .where(
            *_OPEN,
            FollowUp.due_on <= today,
            Prospect.id.not_in(no_follow),
            ~and_(FollowUp.prospect_id.is_not(None), Prospect.status != "open"),
        )
    )


def list_due(db: Session, *, today: date | None = None, member_id: int | None = None) -> DuePanel:
    """Open reminders due today / overdue (prospects with ``no_follow_up`` excluded).

    ``member_id`` keeps only prospects whose latest actor is that team member."""
    today = today or _today()
    stmt = _due_query(today).order_by(FollowUp.due_on, FollowUp.id)
    if member_id is not None:
        stmt = stmt.where(Prospect.actor_member_id == member_id)
    views = views_for(db, list(db.scalars(stmt)), today)
    return DuePanel(due_today=[v for v in views if v.due_on == today], overdue=[v for v in views if v.due_on < today])


def due_count(db: Session, *, today: date | None = None) -> int:
    today = today or _today()
    return int(db.scalar(select(func.count()).select_from(_due_query(today).subquery())) or 0)


def complete(db: Session, follow_up_id: int, *, actor: Actor = TEAM, note: str | None = None, now: datetime | None = None) -> FollowUpView:
    f = _get(db, follow_up_id)
    stamp = now_or(now)
    if f.done_at is None and f.cancelled_at is None:
        f.done_at, f.done_actor, f.updated_at = stamp, actor.label[:80], stamp
        if note:
            f.note = clip(note, 300)
        db.flush()
        _event(db, f, "follow_up_done", f"{KIND_LABELS.get(f.kind, f.kind)} done" + (f": {note}" if note else ""), actor, now)
        if f.prospect_id and f.kind != "custom":
            from sales0.services.crm import prospects

            prospects.advance_to(db, f.prospect_id, f.kind, actor=actor, now=now, reason=f"{KIND_LABELS[f.kind]} done")
        if f.prospect_id:
            refresh_next(db, f.prospect_id)
    return views_for(db, [f], _today(now))[0]


def complete_up_to(db: Session, prospect_id: int, stage: str, *, actor: Actor, now: datetime | None = None) -> int:
    """Mark the open auto reminders whose stage is not after ``stage`` done (stage moved forward by hand)."""
    from sales0.services.crm.stages import stage_label, stage_rank

    stamp = now_or(now)
    n = 0
    done: list[FollowUp] = []
    for f in db.scalars(select(FollowUp).where(FollowUp.prospect_id == prospect_id, FollowUp.kind != "custom", *_OPEN)):
        if stage_rank(f.kind) <= stage_rank(stage):
            f.done_at, f.done_actor, f.updated_at = stamp, actor.label[:80], stamp
            n += 1
            done.append(f)
    if n:
        db.flush()
        for f in done:
            _event(db, f, "follow_up_done", f"{KIND_LABELS.get(f.kind, f.kind)} done (stage moved to {stage_label(stage)})", actor, stamp,
                   data={"auto": True, "stage": stage})
        refresh_next(db, prospect_id)
    return n


def snooze(db: Session, follow_up_id: int, due_on: date, *, actor: Actor = TEAM, now: datetime | None = None) -> FollowUpView:
    f = _get(db, follow_up_id)
    if not isinstance(due_on, date) or isinstance(due_on, datetime) or f.done_at is not None or f.cancelled_at is not None:
        raise DomainError("followup_invalid", "Choose a valid date for the follow-up.", 400)
    old = f.due_on
    f.due_on, f.updated_at = due_on, now_or(now)
    db.flush()
    _event(db, f, "follow_up_added", f"Follow-up moved from {old.isoformat()} to {due_on.isoformat()}", actor, now,
           data={"snoozed_from": old.isoformat()})
    if f.prospect_id:
        refresh_next(db, f.prospect_id)
    return views_for(db, [f], _today(now))[0]


def add_custom(
    db: Session, *, prospect_id: int | None = None, lead_id: int | None = None, due_on: date, note: str | None = None,
    actor: Actor = TEAM, now: datetime | None = None,
) -> FollowUpView:
    if (prospect_id is None) == (lead_id is None):
        raise ValueError("add_custom needs exactly one of prospect_id / lead_id")
    if not isinstance(due_on, date) or isinstance(due_on, datetime) or not (2000 <= due_on.year <= 2100):
        raise DomainError("followup_invalid", "Choose a valid date for the follow-up.", 400)
    if prospect_id is not None and db.get(Prospect, prospect_id) is None:
        raise not_found()
    if lead_id is not None and db.get(Lead, lead_id) is None:
        raise not_found()
    stamp = now_or(now)
    f = FollowUp(prospect_id=prospect_id, lead_id=lead_id, kind="custom", due_on=due_on, note=clip(note, 300), auto=False,
                 created_at=stamp, updated_at=stamp)
    db.add(f)
    db.flush()
    _event(db, f, "follow_up_added", f"Follow-up added for {due_on.isoformat()}" + (f": {note.strip()}" if note and note.strip() else ""), actor, now)
    if prospect_id is not None:
        refresh_next(db, prospect_id)
    return views_for(db, [f], _today(now))[0]


def cancel_open(
    db: Session, *, prospect_id: int | None = None, lead_id: int | None = None, reason: str,
    actor: Actor = SYSTEM, now: datetime | None = None,
) -> int:
    """Cancel every open reminder of a prospect or lead (kept, with the reason). Returns how many."""
    if (prospect_id is None) == (lead_id is None):
        raise ValueError("cancel_open needs exactly one of prospect_id / lead_id")
    col = FollowUp.prospect_id == prospect_id if prospect_id is not None else FollowUp.lead_id == lead_id
    rows = list(db.scalars(select(FollowUp).where(col, *_OPEN)))
    if not rows:
        return 0
    stamp = now_or(now)
    for f in rows:
        f.cancelled_at, f.cancel_reason, f.updated_at = stamp, reason[:80], stamp
    db.flush()
    body = f"{len(rows)} follow-up(s) cancelled ({reason.replace('_', ' ')})"
    data = {"count": len(rows), "reason": reason, "follow_up_ids": [f.id for f in rows]}
    if prospect_id is not None:
        refresh_next(db, prospect_id)
        timeline.add_event(db, subject_type="prospect", subject_id=prospect_id, kind="follow_up_cancelled", body=body,
                           data=data, actor=actor, occurred_at=stamp, prospect_id=prospect_id)
    else:
        timeline.add_event(db, subject_type="lead", subject_id=lead_id or 0, kind="follow_up_cancelled", body=body,
                           data=data, actor=actor, occurred_at=stamp)
    return len(rows)


def for_prospect(db: Session, prospect_id: int, *, today: date | None = None, done_limit: int = 20) -> list[FollowUpView]:
    """Open reminders first (by date), then the most recent done ones."""
    today = today or _today()
    open_rows = list(db.scalars(select(FollowUp).where(FollowUp.prospect_id == prospect_id, *_OPEN).order_by(FollowUp.due_on, FollowUp.id)))
    done_rows = list(db.scalars(select(FollowUp).where(FollowUp.prospect_id == prospect_id, FollowUp.done_at.is_not(None))
                                .order_by(FollowUp.done_at.desc()).limit(done_limit)))
    return views_for(db, open_rows + done_rows, today)


def for_lead(db: Session, lead_id: int, *, today: date | None = None) -> list[FollowUpView]:
    today = today or _today()
    rows = list(db.scalars(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.cancelled_at.is_(None)).order_by(FollowUp.due_on, FollowUp.id)))
    return views_for(db, rows, today)


__all__ = [
    "add_custom", "cancel_open", "complete", "complete_up_to", "due_count", "for_lead", "for_prospect", "list_due",
    "refresh_next", "schedule_for_contact", "snooze", "views_for",
]
