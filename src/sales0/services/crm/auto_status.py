"""Automatic company status: ONE function (:func:`recalc`) decides where a company should stand from what actually
happened, and it runs after every saved action on the company, its contacts, its enquiries or related records.

How it is triggered: every ``timeline.add_event`` (notes, follow-ups, emails sent, replies, enquiry status changes,
board moves ...) records the company it touched in ``session.info``; just before the session commits, ``recalc`` runs
once per touched company (inside a savepoint: a failure is logged and never costs the action). Changes it makes write
their own timeline entry naming the trigger ("Status updated automatically: Contacted -> Follow-up 1 (triggered by:
Follow-up logged)").

The rules (forward only - the automatic status never moves a company back):
* closed companies (off-ramp) and clients are left alone;
* a prospect's status counts its OUTREACH: the distinct days on which we reached out (an email sent, a logged
  follow-up, a follow-up marked done, a logged first contact, a call). 0 = New, 1 = Contacted, 2 = Follow-up 1,
  3 = Follow-up 2, 4+ = Final follow-up;
* a logged meeting (a follow-up logged as a Meeting, or a meeting event) makes a prospect a Lead / Meeting scheduled;
* lead stages (Assessment, Proposal sent, Interviews, On hold) are a person's call: the automatic status never changes them.

Manual override (``set_override``): a person chose the status (the "..." menu on the company page, or a board drag).
The automatic status then holds until the next REAL outreach event after that moment; from then on it counts outreach
from the status the person chose (so an override to "Contacted" followed by one email = Follow-up 1).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.models import Company, Contact, TimelineEvent
from sales0.db.models.enums_crm import PROSPECT_STAGES
from sales0.logging import get_logger
from sales0.services._common import now_or
from sales0.services.crm.actor import SYSTEM, Actor

log = get_logger("sales0.crm.auto_status")

PENDING_KEY = "auto_status_pending"
RUNNING_KEY = "auto_status_running"
# timeline kinds that can be a real outreach touch (see _is_touch)
TOUCH_KINDS = ("email_sent", "call", "follow_up_done", "note", "stage_change", "meeting")
FOLLOW_UP_HOWS = ("email", "call", "whatsapp", "linkedin", "meeting", "other")

RULES_HELP: dict[str, str] = {
    "new": "New: nobody has reached out yet. Moves to Contacted at the first email, logged contact or follow-up.",
    "contacted": "Contacted: one day of outreach so far. The next email / logged follow-up on another day moves it to Follow-up 1.",
    "follow_up_1": "Follow-up 1: two days of outreach. The next one moves it to Follow-up 2.",
    "follow_up_2": "Follow-up 2: three days of outreach. The next one moves it to Final follow-up.",
    "final_follow_up": "Final follow-up: four or more days of outreach. A logged meeting makes it a lead; close it if they never answer.",
    "meeting_scheduled": "Meeting scheduled: a meeting is booked (set when a meeting is logged or a person moves it here).",
    "assessment": "Assessment: they are assessing us (a trial, a test session, a review). Set by a person.",
    "proposal_sent": "Proposal sent: our proposal is with them. Set by a person (the date is recorded).",
    "interviews": "Interviews: trainer / team interviews after the proposal. Set by a person.",
    "on_hold": "On hold: paused on their side. Set by a person; it stays on the Leads board.",
    "active": "Client: they bought from us. A new enquiry is an upsell.",
}


def enabled() -> bool:
    return bool(get_settings().auto_status)


# ---------------------------------------------------------------------------- what counts
def _data(ev: TimelineEvent) -> dict[str, Any]:
    return ev.data if isinstance(ev.data, dict) else {}


def is_touch(ev: TimelineEvent) -> bool:
    """A REAL outreach event: we reached out (email sent, a logged follow-up / contact / call, a follow-up marked done)."""
    d = _data(ev)
    if ev.kind in ("email_sent", "call", "meeting"):
        return True
    if ev.kind == "follow_up_done":
        return not d.get("auto")  # reminders closed because the stage moved are not outreach
    if ev.kind == "note":
        return d.get("follow_up_note_id") is not None
    if ev.kind == "stage_change":
        return ev.subject_type == "prospect" and bool(d.get("channel"))  # a logged first contact
    return False


def is_meeting(ev: TimelineEvent) -> bool:
    d = _data(ev)
    return ev.kind == "meeting" or (ev.kind == "note" and d.get("follow_up_note_id") is not None and d.get("how") == "meeting")


def _events(db: Session, company_id: int, since: datetime | None) -> list[TimelineEvent]:
    stmt = select(TimelineEvent).where(TimelineEvent.company_id == company_id, TimelineEvent.kind.in_(TOUCH_KINDS))
    if since is not None:
        stmt = stmt.where(TimelineEvent.occurred_at > since)
    return list(db.scalars(stmt.order_by(TimelineEvent.occurred_at, TimelineEvent.id)))


def _day(at: datetime) -> str:
    tz = get_settings().tz
    try:
        return (at.astimezone(tz) if at.tzinfo else at).date().isoformat()
    except (ValueError, OverflowError):
        return at.date().isoformat()


def _touch_day_set(db: Session, evs: list[TimelineEvent]) -> set[str]:
    """Distinct local days of real outreach. A logged follow-up counts on the day it HAPPENED; the "contact logged"
    bookkeeping entry written by the same action as an email / follow-up (within two minutes) is not counted twice."""
    from sales0.db.models import FollowUpNote

    touches = [ev for ev in evs if is_touch(ev)]
    note_ids = [int(_data(ev)["follow_up_note_id"]) for ev in touches if ev.kind == "note" and str(_data(ev).get("follow_up_note_id", "")).isdigit()]
    happened = dict(db.execute(select(FollowUpNote.id, FollowUpNote.happened_on).where(FollowUpNote.id.in_(note_ids))).tuples().all()) if note_ids else {}
    others = [ev.occurred_at for ev in touches if ev.kind != "stage_change"]
    days: set[str] = set()
    for ev in touches:
        if ev.kind == "stage_change" and any(abs((ev.occurred_at - at).total_seconds()) <= 120 for at in others):
            continue
        nid = _data(ev).get("follow_up_note_id")
        if ev.kind == "note" and nid is not None and happened.get(int(nid)) is not None:
            days.add(happened[int(nid)].isoformat())
        else:
            days.add(_day(ev.occurred_at))
    return days


def touch_days(db: Session, company_id: int, since: datetime | None = None) -> list[str]:
    """The distinct local days with real outreach (two emails on one day are one touch)."""
    return sorted(_touch_day_set(db, _events(db, company_id, since)))


def override_active(db: Session, co: Company) -> bool:
    """A manual status that still holds: no real outreach since the person set it."""
    if co.status_override_at is None:
        return False
    return not touch_days(db, co.id, co.status_override_at)


# ---------------------------------------------------------------------------- the one function
def target_for(db: Session, co: Company) -> tuple[str, str] | None:
    """(stage, sub_status) the evidence says, forward only from where the company is; None = stay."""
    if co.stage is None or co.off_ramp or co.stage == "client":
        return None
    since = co.status_override_at
    evs = _events(db, co.id, since)
    days = sorted(_touch_day_set(db, evs))
    if since is not None and not days:
        return None  # the manual override holds
    if co.stage == "prospect":
        if any(is_meeting(ev) for ev in evs):
            return ("lead", "meeting_scheduled")
        base = 0
        if since is not None and co.status_override_stage == "prospect" and co.status_override_sub in PROSPECT_STAGES:
            base = PROSPECT_STAGES.index(co.status_override_sub)
        want = min(base + len(days), len(PROSPECT_STAGES) - 1)
        cur = PROSPECT_STAGES.index(co.sub_status) if co.sub_status in PROSPECT_STAGES else 0
        if want > cur:
            return ("prospect", PROSPECT_STAGES[want])
    return None


def recalc(db: Session, company_id: int, *, trigger: str, actor: Actor = SYSTEM, now: datetime | None = None) -> str | None:
    """Recalculate the company's status; returns a description of the change, or None when nothing moved."""
    from sales0.services.crm import lifecycle, timeline

    co = db.get(Company, company_id)
    if co is None:
        return None
    db.flush()
    db.refresh(co)
    target = target_for(db, co)
    if target is None or target == (co.stage, co.sub_status):
        return None
    stamp = now_or(now)
    before = f"{lifecycle.stage_label(co.stage)} / {lifecycle.sub_label(co.sub_status)}"
    note = f"Automatic (triggered by: {trigger})"
    stage, sub = target
    if stage == "lead":
        lifecycle.mark_lead(db, co.id, stage=sub, actor=actor, note=note, now=stamp)
    else:
        lifecycle.set_prospect_status(db, co.id, sub, actor=actor, note=note, now=stamp)
    db.refresh(co)
    after = f"{lifecycle.stage_label(co.stage)} / {lifecycle.sub_label(co.sub_status)}"
    body = f"Status updated automatically: {before} -> {after} (triggered by: {trigger})"
    timeline.add_event(db, subject_type="company", subject_id=co.id, kind="stage_change", company_id=co.id, actor=actor,
                       body=body, data={"auto": True, "trigger": trigger, "to": {"stage": co.stage, "sub_status": co.sub_status}},
                       occurred_at=stamp)
    log.info("auto_status_changed", company_id=co.id, to=f"{co.stage}/{co.sub_status}", trigger=trigger)
    return body


# ---------------------------------------------------------------------------- manual override
def set_override(db: Session, company_id: int, *, actor: Actor, now: datetime | None = None) -> None:
    """A person chose the company's status (after the move): hold the automatic status until the next real outreach."""
    co = db.get(Company, company_id)
    if co is None:
        return
    db.flush()
    db.refresh(co)
    co.status_override_at = now_or(now)
    co.status_override_by = actor.label[:80]
    co.status_override_stage, co.status_override_sub = co.stage, co.sub_status
    db.flush()


def clear_override(db: Session, company_id: int) -> None:
    co = db.get(Company, company_id)
    if co is not None:
        co.status_override_at = co.status_override_by = co.status_override_stage = co.status_override_sub = None
        db.flush()


# ---------------------------------------------------------------------------- the trigger
TRIGGER_LABELS = {
    "email_sent": "Email sent", "reply_received": "Reply received", "follow_up_done": "Follow-up done",
    "follow_up_added": "Follow-up added", "call": "Call logged", "meeting": "Meeting logged", "research": "Research",
    "enquiry_linked": "Enquiry linked", "created": "Record created", "lead_created": "Lead created",
    "lead_stage": "Lead stage changed", "proposal_sent": "Proposal sent", "client_created": "Client created",
    "contact_linked": "Contact linked", "contact_merged": "Contacts merged", "flag_set": "Flag set",
}
SUBJECT_WORDS = {"enquiry": "an enquiry", "contact": "a contact", "company": "the company", "prospect": "the company",
                 "lead": "the lead", "client": "the client"}


def trigger_label(ev: TimelineEvent) -> str:
    d = _data(ev)
    if ev.kind == "note":
        what = "Follow-up logged" if d.get("follow_up_note_id") is not None else "Note saved"
    elif ev.kind == "stage_change":
        what = "Contact logged" if d.get("channel") else "Status change"
    else:
        what = TRIGGER_LABELS.get(ev.kind, ev.kind.replace("_", " ").capitalize())
    where = SUBJECT_WORDS.get(ev.subject_type)
    return f"{what} on {where}" if where and ev.subject_type not in ("prospect", "company") else what


# bulk bookkeeping (sheet sync, imports) never triggers a recalculation: nobody acted on the company
QUIET_KINDS = frozenset({"sheet_created", "sheet_updated", "sheet_removed", "sheet_restored", "sheet_status", "import"})


def note_event(db: Session, ev: TimelineEvent) -> None:
    """Called by ``timeline.add_event``: remember the company to recalculate before the commit."""
    if db.info.get(RUNNING_KEY) or ev.kind in QUIET_KINDS:
        return
    cid = ev.company_id
    if cid is None and ev.contact_id is not None:
        cid = db.scalar(select(Contact.company_id).where(Contact.id == ev.contact_id))
    if cid is None:
        return
    pending: dict[int, str] = db.info.setdefault(PENDING_KEY, {})
    label = trigger_label(ev)
    # a real outreach event names the trigger over the bookkeeping entries of the same action
    if cid not in pending or is_touch(ev) or is_meeting(ev):
        pending[cid] = label


def run_pending(db: Session) -> int:
    pending: dict[int, str] = db.info.pop(PENDING_KEY, {}) or {}
    if not pending or not enabled():
        return 0
    changed = 0
    db.info[RUNNING_KEY] = True
    try:
        for cid, trigger in sorted(pending.items()):
            try:
                with db.begin_nested():
                    if recalc(db, cid, trigger=trigger):
                        changed += 1
            except Exception as exc:  # noqa: BLE001 - the automatic status must never cost the action
                log.warning("auto_status_failed", company_id=cid, error=f"{type(exc).__name__}: {exc}"[:300])
    finally:
        db.info.pop(RUNNING_KEY, None)
    return changed


_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    @event.listens_for(Session, "before_commit")
    def _before_commit(session: Session) -> None:
        if session.info.get(PENDING_KEY) and not session.info.get(RUNNING_KEY):
            run_pending(session)

    @event.listens_for(Session, "after_rollback")
    def _after_rollback(session: Session) -> None:
        if not session.info.get(RUNNING_KEY):
            session.info.pop(PENDING_KEY, None)


install()

__all__ = ["RULES_HELP", "clear_override", "enabled", "is_touch", "note_event", "override_active", "recalc", "run_pending",
           "set_override", "target_for", "touch_days"]
