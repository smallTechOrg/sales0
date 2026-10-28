"""Event subscribers: the CRM reacts to enquiry decisions, sent messages and replies.

Each handler runs in its own savepoint and swallows (logs) every failure, so the publisher
(inbox decide, message send, reply poller) is never broken by the CRM.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.clock import local_today
from sales0.config import get_settings
from sales0.db.models import Enquiry, FollowUp, Lead, OutreachMessage, OutreachReply, Prospect, TeamMember, TimelineEvent
from sales0.logging import get_logger
from sales0.services import events
from sales0.services.crm import contacts, flags, followups, leads, prospects, timeline
from sales0.services.crm.actor import SYSTEM, TEAM, Actor

log = get_logger("sales0.crm.hooks")


def _guarded(fn: Callable[[Session, Any], None]) -> Callable[[Session, Any], None]:
    def wrapper(db: Session, ev: Any) -> None:
        try:
            with db.begin_nested():
                fn(db, ev)
        except Exception:  # noqa: BLE001 - the CRM must never break its publisher
            log.exception("crm_hook_failed", hook=fn.__name__, event_type=type(ev).__name__)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
    return wrapper


def _member_actor(db: Session, member_id: int | None) -> Actor:
    m = db.get(TeamMember, member_id) if member_id else None
    return Actor.of_member(m)


# ---------------------------------------------------------------------------- enquiry decided
def _on_enquiry_decided(db: Session, ev: events.EnquiryDecided) -> None:
    actor = _member_actor(db, ev.actor_member_id)
    e = db.get(Enquiry, ev.enquiry_id)
    if e is None:
        return
    if ev.decision == "accept":
        prospects.ensure_for_enquiry(db, ev.enquiry_id, actor=actor)
        return
    if ev.decision == "discard" and e.prospect_id:
        p = db.get(Prospect, e.prospect_id)
        if p is None or p.status != "open" or p.stage != "new" or p.stage_source == "sheet":
            return
        other = db.scalar(
            select(func.count(Enquiry.id)).where(Enquiry.prospect_id == p.id, Enquiry.id != e.id, Enquiry.review_status == "accepted")
        )
        if not other:
            prospects.close(db, p.id, "enquiry_discarded", actor=actor)


# ---------------------------------------------------------------------------- message sent
def _on_message_sent(db: Session, ev: events.MessageSent) -> None:
    msg = db.get(OutreachMessage, ev.outreach_message_id)
    if msg is None or msg.state != "sent":
        return
    pid = prospects.resolve_for_message(db, msg.id)
    if pid is None and msg.enquiry_id:
        e = db.get(Enquiry, msg.enquiry_id)
        if e is not None and e.review_status == "accepted":
            pid = prospects.ensure_for_enquiry(db, e.id, actor=_member_actor(db, ev.member_id), now=ev.sent_at).id
    if pid is None:
        return
    p = db.get(Prospect, pid)
    if p is None:
        return
    actor = _member_actor(db, ev.member_id)
    if msg.prospect_id is None:
        msg.prospect_id = p.id
    if msg.company_id is None and p.company_id:
        msg.company_id = p.company_id
    prior_emails = int(db.scalar(
        select(func.count(TimelineEvent.id)).where(TimelineEvent.prospect_id == p.id, TimelineEvent.kind == "email_sent")
    ) or 0)
    already = any(
        (t.data or {}).get("message_id") == msg.id
        for t in db.scalars(select(TimelineEvent).where(TimelineEvent.prospect_id == p.id, TimelineEvent.kind == "email_sent"))
    )
    if already:
        return
    timeline.add_event(
        db, subject_type="prospect", subject_id=p.id, kind="email_sent", body=msg.subject or None,
        data={"message_id": msg.id, "to": list(ev.to_addrs)}, actor=actor, occurred_at=ev.sent_at,
    )
    for addr in ev.to_addrs:
        try:
            row, _ = contacts.upsert_row(db, email=addr, company_id=p.company_id, origin="enquiry", actor=SYSTEM, now=ev.sent_at)
        except Exception:  # noqa: BLE001 - an unparsable recipient must not stop the stage move
            continue
        contacts.link(db, row.id, "prospect", p.id, "recipient")
        if p.contact_id is None:
            p.contact_id = row.id
    e = db.get(Enquiry, msg.enquiry_id) if msg.enquiry_id else (db.get(Enquiry, p.origin_enquiry_id) if p.origin_enquiry_id else None)
    if e is not None and e.date_email_sent is None:
        e.date_email_sent = local_today(get_settings().tz, ev.sent_at)
    db.flush()
    if p.status != "open":
        # closed / converted: the email is still a real contact - record it, change no status, schedule nothing
        prospects.record_email_contact(db, p.id, when=ev.sent_at, actor=actor, schedule=False, now=ev.sent_at)
        return
    if p.stage == "new":
        prospects.mark_contacted(db, p.id, when=ev.sent_at, actor=actor, channel="email", now=ev.sent_at)
    elif prior_emails == 0:
        # contacted by hand (LinkedIn/call/sheet) before: the first app email is not a follow-up by itself
        prospects.record_email_contact(db, p.id, when=ev.sent_at, actor=actor, now=ev.sent_at)
        any_auto = db.scalar(select(FollowUp.id).where(FollowUp.prospect_id == p.id, FollowUp.auto.is_(True)).limit(1))
        if any_auto is None:
            followups.schedule_for_contact(db, p.id, p.contacted_at or ev.sent_at, now=ev.sent_at)
    else:
        earliest = db.scalar(
            select(FollowUp).where(FollowUp.prospect_id == p.id, FollowUp.done_at.is_(None), FollowUp.cancelled_at.is_(None), FollowUp.kind != "custom")
            .order_by(FollowUp.due_on, FollowUp.id).limit(1)
        )
        if earliest is not None:
            followups.complete(db, earliest.id, actor=actor, note="follow-up email sent", now=ev.sent_at)


# ---------------------------------------------------------------------------- reply received
def _on_reply_received(db: Session, ev: events.ReplyReceived) -> None:
    pid = prospects.resolve_for_message(db, ev.outreach_message_id)
    if pid is None:
        return
    p = db.get(Prospect, pid)
    if p is None:
        return
    if ev.reply_id is not None:
        for t in db.scalars(select(TimelineEvent).where(TimelineEvent.prospect_id == p.id, TimelineEvent.kind.in_(("reply_received", "note")))):
            if (t.data or {}).get("reply_id") == ev.reply_id:
                return  # already handled (idempotent)
    reply = db.get(OutreachReply, ev.reply_id) if ev.reply_id else None
    stamp = reply.received_at if reply is not None else None
    if ev.kind != "human":
        label = "Bounce received (delivery failed)" if ev.kind == "bounce" else "Automatic reply received (not counted as a response)"
        timeline.add_event(
            db, subject_type="prospect", subject_id=p.id, kind="note", body=label,
            data={"system": True, "reply_id": ev.reply_id, "reply_kind": ev.kind}, actor=SYSTEM, occurred_at=stamp,
        )
        return
    prospects.record_response(
        db, p.id, actor=SYSTEM, source="reply", reply_id=ev.reply_id, now=stamp,
        note=f"intent: {ev.intent}" if ev.intent else None, data={"intent": ev.intent, "from": ev.from_addr or None},
    )
    if ev.intent == "unsubscribe":
        flags.set_flag(db, "prospect", p.id, "no_follow_up", source="auto",
                       detail={"reason": "unsubscribe", "reply_id": ev.reply_id}, actor=SYSTEM, now=stamp)
        return
    if ev.intent in ("interested", "question", "not_now"):
        _open_lead_for_reply(db, p, ev, stamp)


def _open_lead_for_reply(db: Session, p: Prospect, ev: events.ReplyReceived, stamp: Any) -> None:
    """A human reply after contact opens a lead (reopening a closed prospect); never a silent skip."""
    db.refresh(p)
    if db.scalar(select(Lead.id).where(Lead.prospect_id == p.id, Lead.closed_at.is_(None))) is not None:
        return
    if p.contacted_at is None:
        timeline.add_event(
            db, subject_type="prospect", subject_id=p.id, kind="note", actor=SYSTEM, occurred_at=stamp,
            body="Reply recorded but no lead opened: the prospect has no recorded contact.",
            data={"system": True, "reply_id": ev.reply_id, "intent": ev.intent},
        )
        return
    if p.status == "closed":
        was = p.closed_reason
        prospects.reopen(db, p.id, actor=SYSTEM, now=stamp)
        timeline.add_event(
            db, subject_type="prospect", subject_id=p.id, kind="note", actor=SYSTEM, occurred_at=stamp,
            body=f"Reopened automatically: a {ev.intent.replace('_', ' ')} reply arrived after the prospect was closed"
            + (f" ({was.replace('_', ' ')})." if was else "."),
            data={"system": True, "reply_id": ev.reply_id, "intent": ev.intent, "was_closed_reason": was},
        )
        db.refresh(p)
    if p.status in ("open", "converted"):
        leads.convert(db, p.id, stage="meeting_scheduled", opened_by="reply", actor=SYSTEM, note=ev.intent, now=stamp)


# ---------------------------------------------------------------------------- tracked profile hit (prospect watch)
def _needs_follow_up(db: Session, p: Prospect) -> bool:
    """Assumed rule: a hit on a prospect we had given up on (or ran out of reminders for) needs a human follow-up.

    True for: closed as no_response / lost / not_interested; open at the final follow-up stage; converted whose
    latest lead is lost / no_response. New / contacted / earlier follow-ups, open leads and won leads just get the note.
    """
    if p.status == "closed":
        return p.closed_reason in prospects.UNTRACK_REASONS
    if p.status == "open":
        return p.stage == "final_follow_up"
    ld = db.scalar(select(Lead).where(Lead.prospect_id == p.id).order_by(Lead.id.desc()).limit(1))
    return ld is not None and ld.stage in ("lost", "no_response")


def _on_tracked_profile_hit(db: Session, ev: events.TrackedProfileHit) -> None:
    p = db.scalar(select(Prospect).where(Prospect.linkedin_profile_id == ev.profile_id))
    e = db.get(Enquiry, ev.enquiry_id)
    if p is None or e is None:
        return
    for t in db.scalars(select(TimelineEvent).where(TimelineEvent.prospect_id == p.id, TimelineEvent.kind == "note")):
        if (t.data or {}).get("watch_enquiry_id") == e.id:
            return  # idempotent: one note per hit
    if e.prospect_id is None and e.review_status == "accepted":
        e.prospect_id = p.id  # a pending enquiry is linked by the normal accept flow (identity = the profile)
    flagged = False
    if _needs_follow_up(db, p) and flags._active(db, "prospect", p.id, "no_follow_up") is None:  # noqa: SLF001
        flagged = True
    timeline.add_event(
        db, subject_type="prospect", subject_id=p.id, kind="note", prospect_id=p.id, enquiry_id=e.id, company_id=p.company_id,
        body="New requirement from tracked profile" + (" - follow-up needed" if flagged else ""),
        data={"system": True, "watch_enquiry_id": e.id, "run_id": ev.run_id, "follow_up_needed": flagged}, actor=SYSTEM,
    )
    if flagged:
        followups.add_custom(
            db, prospect_id=p.id, due_on=local_today(get_settings().tz), actor=SYSTEM,
            note="New requirement from a tracked profile - follow up",
        )


on_enquiry_decided = _guarded(_on_enquiry_decided)
on_message_sent = _guarded(_on_message_sent)
on_reply_received = _guarded(_on_reply_received)
on_tracked_profile_hit = _guarded(_on_tracked_profile_hit)

__all__ = ["TEAM", "on_enquiry_decided", "on_message_sent", "on_reply_received", "on_tracked_profile_hit"]
