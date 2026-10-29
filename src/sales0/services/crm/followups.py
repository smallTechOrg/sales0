"""Follow-up reminders: schedule on contact, due-today panel, complete (with the channel) / reschedule / custom / cancel.

The three auto reminders (Follow-up 1, 2, Final) exist from the first contact on, each with an editable date. Before
the first contact they are only a PLAN (``plan``: contact day + SALES0_FOLLOWUP_DAYS_*, shown in the Log contact form,
editable there); they become rows when the contact is logged (or an email is sent). Assumed: planned dates are not
stored before contact - the agent adjusts them in the Log contact form or inline afterwards.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from sales0.clock import local_today
from sales0.config import get_settings
from sales0.db.models import Flag, FollowUp, Lead, OutreachMessage, Prospect
from sales0.db.models.enums_crm import FOLLOW_UP_CHANNELS
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
AUTO_KINDS = ("follow_up_1", "follow_up_2", "final_follow_up")
CHANNEL_LABELS = {"whatsapp": "WhatsApp", "linkedin": "LinkedIn", "email": "Email", "call": "Call"}
assert tuple(CHANNEL_LABELS) == FOLLOW_UP_CHANNELS


DEFAULT_PLANNED_CHANNEL = "email"


def check_channel(channel: str | None, *, required: bool = False) -> str | None:
    """A valid channel key (lowercased) or None; ``followup_invalid`` otherwise."""
    value = (channel or "").strip().lower() or None
    if value is None and required:
        raise DomainError("followup_invalid", "Choose how it was done: WhatsApp, LinkedIn, Email or Call.", 400)
    if value is not None and value not in CHANNEL_LABELS:
        raise DomainError("followup_invalid", "Choose how it was done: WhatsApp, LinkedIn, Email or Call.", 400)
    return value


def _check_date(d: object) -> date:
    if not isinstance(d, date) or isinstance(d, datetime) or not (2000 <= d.year <= 2100):
        raise DomainError("followup_invalid", "Choose a valid date for the follow-up.", 400)
    return d


def plan(contact_day: date) -> list[tuple[str, str, date]]:
    """(kind, label, due date) of the three auto reminders for a contact on ``contact_day`` (settings offsets)."""
    s = get_settings()
    days = (s.followup_days_1, s.followup_days_2, s.followup_days_final)
    return [(k, KIND_LABELS[k], contact_day + timedelta(days=n)) for k, n in zip(AUTO_KINDS, days, strict=True)]


def _today(now: datetime | None = None) -> date:
    return local_today(get_settings().tz, now)


def _view(f: FollowUp, who: str, today: date) -> FollowUpView:
    return FollowUpView(
        id=f.id, prospect_id=f.prospect_id, lead_id=f.lead_id, who=who, kind=f.kind,
        kind_label=KIND_LABELS.get(f.kind, f.kind), due_on=f.due_on, note=f.note,
        overdue=(f.done_at is None and f.cancelled_at is None and f.due_on < today),
        auto=bool(f.auto), done=f.done_at is not None, done_at=f.done_at, channel=f.channel,
        channel_label=CHANNEL_LABELS.get(f.channel or "", None),
        planned_channel=f.planned_channel or "email", planned_channel_label=CHANNEL_LABELS.get(f.planned_channel or "email", "Email"),
    )


def _draft_map(db: Session, ids: list[int]) -> dict[int, tuple[int, str]]:
    """follow_up id -> (newest follow-up email message id, its state) for the reminders that have an email draft."""
    if not ids:
        return {}
    out: dict[int, tuple[int, str]] = {}
    for fid, mid, state in db.execute(
        select(OutreachMessage.follow_up_id, OutreachMessage.id, OutreachMessage.state)
        .where(OutreachMessage.follow_up_id.in_(ids)).order_by(OutreachMessage.id)
    ).all():
        out[fid] = (mid, state)
    return out


def views_for(db: Session, rows: list[FollowUp], today: date | None = None) -> list[FollowUpView]:
    today = today or _today()
    pids = {r.prospect_id for r in rows if r.prospect_id}
    lids = {r.lead_id for r in rows if r.lead_id}
    names: dict[int, str] = dict(db.execute(select(Prospect.id, Prospect.name).where(Prospect.id.in_(pids))).all()) if pids else {}
    lead_names: dict[int, str] = (
        dict(db.execute(select(Lead.id, Prospect.name).join(Prospect, Prospect.id == Lead.prospect_id).where(Lead.id.in_(lids))).all())
        if lids else {}
    )
    drafts = _draft_map(db, [r.id for r in rows])
    out = [_view(r, names.get(r.prospect_id, "") if r.prospect_id else lead_names.get(r.lead_id, ""), today) for r in rows]
    return [v.model_copy(update={"draft_message_id": drafts[v.id][0], "draft_state": drafts[v.id][1]}) if v.id in drafts else v for v in out]


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


def schedule_for_contact(
    db: Session, prospect_id: int, contacted_at: datetime, *, now: datetime | None = None, due_dates: dict[str, date] | None = None,
    channels: dict[str, str] | None = None,
) -> list[FollowUpView]:
    """The three reminders (calendar days from the contact date, or the agent's ``due_dates`` per kind; ``channels`` =
    the planned channel per kind, default email). No-op with ``no_follow_up`` or open auto reminders."""
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
    created: list[FollowUp] = []
    for kind, _label, default in plan(base):
        due = _check_date((due_dates or {}).get(kind) or default)
        f = FollowUp(prospect_id=p.id, kind=kind, due_on=due, auto=True, created_at=stamp, updated_at=stamp,
                     planned_channel=check_channel((channels or {}).get(kind)) or DEFAULT_PLANNED_CHANNEL)
        db.add(f)
        created.append(f)
    db.flush()
    refresh_next(db, p.id)
    timeline.add_event(
        db, subject_type="prospect", subject_id=p.id, kind="follow_up_added",
        body="Follow-ups scheduled: " + ", ".join(f"{KIND_LABELS[f.kind]} on {f.due_on.isoformat()}" for f in created),
        data={"follow_up_ids": [f.id for f in created], "due": [f.due_on.isoformat() for f in created],
              "channels": [f.planned_channel for f in created]},
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


def complete(
    db: Session, follow_up_id: int, *, actor: Actor = TEAM, note: str | None = None, channel: str | None = None,
    now: datetime | None = None,
) -> FollowUpView:
    """Mark done, recording HOW (``channel``) and a note; an auto reminder moves the prospect to its stage."""
    f = _get(db, follow_up_id)
    channel = check_channel(channel)
    note = (note or "").strip() or None
    stamp = now_or(now)
    if f.done_at is None and f.cancelled_at is None:
        f.done_at, f.done_actor, f.updated_at, f.channel = stamp, actor.label[:80], stamp, channel
        if note:
            f.note = clip(note, 300)
        db.flush()
        how = f" via {CHANNEL_LABELS[channel]}" if channel else ""
        _event(db, f, "follow_up_done", f"{KIND_LABELS.get(f.kind, f.kind)} done{how}" + (f": {note}" if note else ""), actor, now,
               data={**({"channel": channel} if channel else {}), **({"note": note} if note else {})})
        stop_drafts(db, [f.id], f"{KIND_LABELS.get(f.kind, f.kind)} was done{how}", now=stamp)  # a pending draft is moot now
        if f.prospect_id and f.kind != "custom":
            from sales0.services.crm import prospects

            prospects.advance_to(db, f.prospect_id, f.kind, actor=actor, now=now, reason=f"{KIND_LABELS[f.kind]} done{how}",
                                 extra={"channel": channel} if channel else None)
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
        stop_drafts(db, [f.id for f in done], f"stage moved to {stage_label(stage)}", now=stamp)
        for f in done:
            _event(db, f, "follow_up_done", f"{KIND_LABELS.get(f.kind, f.kind)} done (stage moved to {stage_label(stage)})", actor, stamp,
                   data={"auto": True, "stage": stage})
        refresh_next(db, prospect_id)
    return n


def snooze(db: Session, follow_up_id: int, due_on: date, *, actor: Actor = TEAM, now: datetime | None = None) -> FollowUpView:
    """Change an open reminder's date (Snooze and the inline date edit). Same date = no-op."""
    f = _get(db, follow_up_id)
    if not isinstance(due_on, date) or isinstance(due_on, datetime) or f.done_at is not None or f.cancelled_at is not None:
        raise DomainError("followup_invalid", "Choose a valid date for the follow-up.", 400)
    _check_date(due_on)
    old = f.due_on
    if old == due_on:
        return views_for(db, [f], _today(now))[0]
    f.due_on, f.updated_at = due_on, now_or(now)
    db.flush()
    _event(db, f, "follow_up_added", f"{KIND_LABELS.get(f.kind, f.kind)} moved from {old.isoformat()} to {due_on.isoformat()}", actor, now,
           data={"snoozed_from": old.isoformat()})
    if f.prospect_id:
        refresh_next(db, f.prospect_id)
    return views_for(db, [f], _today(now))[0]


def set_planned_channel(
    db: Session, follow_up_id: int, channel: str, *, actor: Actor = TEAM, now: datetime | None = None,
) -> FollowUpView:
    """Change HOW an open reminder is planned to be done (WhatsApp / LinkedIn / Email / Call). Only Email ones are
    AI-drafted; switching away from Email rejects a pending draft for it. Same channel = no-op."""
    f = _get(db, follow_up_id)
    new = check_channel(channel, required=True)
    if f.done_at is not None or f.cancelled_at is not None:
        raise DomainError("followup_invalid", "This follow-up is already finished.", 400)
    old = f.planned_channel or DEFAULT_PLANNED_CHANNEL
    if old == new:
        return views_for(db, [f], _today(now))[0]
    f.planned_channel, f.updated_at = new, now_or(now)
    db.flush()
    _event(db, f, "follow_up_added", f"{KIND_LABELS.get(f.kind, f.kind)} will be done via {CHANNEL_LABELS[new]} (was {CHANNEL_LABELS.get(old, old)})",
           actor, now, data={"planned_channel": new, "was": old})
    if new != "email":
        stop_drafts(db, [f.id], f"planned channel changed to {CHANNEL_LABELS[new]}", now=now)
    return views_for(db, [f], _today(now))[0]


def stop_drafts(db: Session, follow_up_ids: list[int], why: str, *, now: datetime | None = None) -> int:
    """Reject the NOT-yet-sent email drafts of these reminders (a reply arrived, it was done by hand, ...). Never touches
    a message that is sending/sent. Returns how many drafts were rejected."""
    if not follow_up_ids:
        return 0
    from sales0.services.outreach._shared import add_event

    n = 0
    for m in db.scalars(select(OutreachMessage).where(OutreachMessage.follow_up_id.in_(follow_up_ids),
                                                     OutreachMessage.state.in_(("draft", "edited", "approved", "failed")))):
        if m.state == "failed" and m.send_outcome_unknown:
            continue  # the outcome is unknown: a human decides in the outbox
        m.state, m.approved_at, m.approved_by_member_id, m.content_sha256 = "rejected", None, None, None
        m.error = f"Follow-up stopped: {why}."[:1000]
        m.updated_at = now_or(now)
        add_event(db, m.id, "follow_up_stopped", None, why, now)
        n += 1
    if n:
        db.flush()
    return n


def add_custom(
    db: Session, *, prospect_id: int | None = None, lead_id: int | None = None, due_on: date, note: str | None = None,
    actor: Actor = TEAM, now: datetime | None = None, planned_channel: str | None = None,
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
                 created_at=stamp, updated_at=stamp, planned_channel=check_channel(planned_channel) or DEFAULT_PLANNED_CHANNEL)
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
    stop_drafts(db, [f.id for f in rows], reason.replace("_", " "), now=stamp)
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
    "AUTO_KINDS", "CHANNEL_LABELS", "add_custom", "cancel_open", "check_channel", "complete", "complete_up_to", "due_count",
    "for_lead", "for_prospect", "list_due", "plan", "refresh_next", "schedule_for_contact", "set_planned_channel", "snooze",
    "stop_drafts", "views_for",
]
