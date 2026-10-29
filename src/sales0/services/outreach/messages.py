"""Outreach message state machine. This module is the ONLY place that sends email.

    draft -> edited -> approved -> sending -> sent
                          |            \\-> failed (retry; or unknown outcome -> explicit retry_send)
              (any editable) -> rejected

Rules (spec: capabilities/email-outreach.md):
* Only the message's owner may edit / approve / reject / send it (admins may view all). Assumed.
* Approval binds to ``DraftContent.sha256`` of the exact final content INCLUDING the deck bytes.
  Editing an approved message resets it to ``edited`` and clears the approval.
* ``send`` claims the message with a compare-and-swap UPDATE (approved -> sending) and COMMITS
  the claim before any Gmail call, so a double click or two threads send once. It then creates
  the Gmail draft from the stored content and calls ``send_draft`` with an ``ApprovedMessage``;
  the adapter re-fetches the draft and refuses on a hash mismatch.
* Never retried automatically. A send whose outcome is unknowable (network/5xx during
  ``send_draft``) is ``failed`` with ``send_outcome_unknown`` and needs ``retry_send(...,
  confirm_not_sent=True)`` - a human checked the Sent folder.

``send`` / ``retry_send`` / ``approve`` / ``update_message`` / ``reject`` commit their own
transitions (the claim must be visible to other sessions). Pass a session you own.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from sales0.clock import local_day_bounds_utc
from sales0.config import Settings, get_settings
from sales0.db.models import Deck, OutreachEvent, OutreachMessage, TeamMember
from sales0.errors import DomainError
from sales0.integrations.gmail.client import ApprovedMessage, GmailClient, prepare
from sales0.integrations.gmail.errors import (
    ApprovalMismatch,
    GmailApiError,
    GmailAuthError,
    GmailError,
    GmailNotConfigured,
    GmailNotFound,
    InvalidMessage,
)
from sales0.integrations.gmail.mime import DraftContent, validate_address
from sales0.logging import get_logger
from sales0.services._common import clip, not_found, now_or
from sales0.services import events, suppressions
from sales0.services.outreach import decks, templates
from sales0.services.outreach._shared import acted_by_of, add_event, join_addrs, split_addrs
from sales0.services.team import connections
from sales0.services.team.members import normalise_email

log = get_logger("sales0.outreach.messages")

EDITABLE_STATES: Final = ("draft", "edited", "approved", "failed")


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final = _Unset()


# ----------------------------------------------------------------------------- queries
def get_message(db: Session, msg_id: int, member: TeamMember | None = None) -> OutreachMessage:
    """The message. With ``member`` given, non-admins may only read their own (admins read all)."""
    msg = db.get(OutreachMessage, msg_id)
    if msg is None:
        raise not_found()
    if member is not None and member.role != "admin" and msg.member_id != member.id:
        raise not_found()
    return msg


def list_events(db: Session, msg_id: int) -> list[OutreachEvent]:
    return list(
        db.execute(
            select(OutreachEvent).where(OutreachEvent.message_id == msg_id).order_by(OutreachEvent.id)
        ).scalars()
    )


def list_outbox(
    db: Session,
    member: TeamMember | None = None,
    state: str | list[str] | tuple[str, ...] | None = None,
    *,
    everyone: bool = False,
    limit: int = 200,
) -> list[OutreachMessage]:
    """Newest first. ``member=None`` -> every member's messages; a member -> their own, unless an
    admin passes ``everyone=True``."""
    stmt = select(OutreachMessage).order_by(OutreachMessage.updated_at.desc(), OutreachMessage.id.desc()).limit(limit)
    if member is not None and not (everyone and member.role == "admin"):
        stmt = stmt.where(OutreachMessage.member_id == member.id)
    if state:
        states = [state] if isinstance(state, str) else list(state)
        stmt = stmt.where(OutreachMessage.state.in_(states))
    return list(db.execute(stmt).scalars())


def member_sends_today(db: Session, member_id: int, *, now: datetime | None = None, settings: Settings | None = None) -> int:
    """Messages this member sent (or has in flight) during the current local day."""
    settings = settings or get_settings()
    start, end = local_day_bounds_utc(settings.tz, now)
    return int(
        db.execute(
            select(func.count())
            .select_from(OutreachMessage)
            .where(
                OutreachMessage.member_id == member_id,
                or_(
                    and_(OutreachMessage.state == "sent", OutreachMessage.sent_at >= start, OutreachMessage.sent_at < end),
                    and_(
                        OutreachMessage.state == "sending",
                        OutreachMessage.updated_at >= start,
                        OutreachMessage.updated_at < end,
                    ),
                ),
            )
        ).scalar_one()
    )


# ----------------------------------------------------------------------------- helpers
def _load(db: Session, msg_id: int) -> OutreachMessage:
    msg = db.get(OutreachMessage, msg_id)
    if msg is None:
        raise not_found()
    return msg


def _require_owner(msg: OutreachMessage, member: TeamMember, action: str) -> None:
    if msg.member_id != member.id:
        if member.role != "admin":
            raise not_found()  # same answer as a nonexistent id: no message-id enumeration
        raise DomainError("not_owner", f"Only the member who owns this message can {action} it.", 403)


def _build_content(db: Session, msg: OutreachMessage, settings: Settings | None = None) -> DraftContent:
    """The message exactly as Gmail will hold it (deck bytes attached): what the approval hashes."""
    attachments: list[tuple[str, bytes, str]] = []
    if msg.deck_id is not None:
        deck = db.get(Deck, msg.deck_id)
        if deck is None:
            raise DomainError("deck_missing", "The attached deck no longer exists - pick another or remove it.", 409)
        attachments.append((deck.filename, decks.read_deck_bytes(deck, settings), decks.PDF_MIME))
    try:
        return prepare(split_addrs(msg.to_addrs), split_addrs(msg.cc_addrs), msg.subject, msg.body, None, attachments)
    except InvalidMessage as exc:
        raise DomainError("invalid_message", str(exc), 400) from exc


def _attachment_note(db: Session, msg: OutreachMessage) -> str:
    """'attachment=name.pdf (120 KB)' / 'no attachment' for the audit trail (approval + sent records)."""
    deck = db.get(Deck, msg.deck_id) if msg.deck_id is not None else None
    return f"attachment={decks.attachment_label(deck)} deck={deck.id} sha256={deck.sha256[:12]}" if deck is not None else "no attachment"


def _reset_approval(msg: OutreachMessage) -> None:
    msg.content_sha256 = None
    msg.approved_by_member_id = None
    msg.approved_at = None
    msg.retry_decided_by = None


# ----------------------------------------------------------------------------- edit / approve / reject
def update_message(
    db: Session,
    msg_id: int,
    member: TeamMember,
    *,
    to: list[str] | str | None = None,
    cc: list[str] | str | None = None,
    subject: str | None = None,
    body: str | None = None,
    deck_id: int | None | _Unset = UNSET,
    now: datetime | None = None,
) -> OutreachMessage:
    """Edit a message (``None`` = leave unchanged; ``deck_id=None`` detaches the deck).

    Allowed in draft / edited / approved (an approved message drops back to ``edited`` and loses
    its approval) / failed with a known outcome. Forbidden once sending or sent, and while a
    failed send's outcome is unknown. A call that changes nothing is a no-op.
    """
    msg = _load(db, msg_id)
    _require_owner(msg, member, "edit")
    if msg.state not in EDITABLE_STATES or (msg.state == "failed" and msg.send_outcome_unknown):
        raise DomainError("not_editable", f"A message that is {msg.state} can no longer be edited.", 409)
    new_to = join_addrs(split_addrs(to)) if to is not None else msg.to_addrs
    new_cc = join_addrs(split_addrs(cc)) if cc is not None else msg.cc_addrs
    new_subject = " ".join(subject.split()) if subject is not None else msg.subject
    new_body = body.replace("\r\n", "\n").replace("\r", "\n").strip() if body is not None else msg.body
    if len(new_subject) > 300:
        raise DomainError("subject_too_long", "The subject is longer than 300 characters.", 400)
    new_deck: int | None = msg.deck_id
    if not isinstance(deck_id, _Unset):
        new_deck = deck_id
        if deck_id is not None and deck_id != msg.deck_id:
            chosen = decks.get_deck(db, deck_id)
            if not chosen.active:
                raise DomainError("deck_missing", "That deck was deleted - pick another.", 409)
            decks.read_deck_bytes(chosen)  # deck_missing / deck_corrupt now, not at approval
    if new_to != msg.to_addrs or new_cc != msg.cc_addrs:
        suppressions.assert_not_suppressed(db, new_to, new_cc)
    changed = [
        name
        for name, old, new in (
            ("to", msg.to_addrs, new_to), ("cc", msg.cc_addrs, new_cc), ("subject", msg.subject, new_subject),
            ("body", msg.body, new_body), ("deck", msg.deck_id, new_deck),
        )  # fmt: skip
        if old != new
    ]
    if not changed:
        return msg
    stamp = now_or(now)
    was_approved = msg.state == "approved"
    msg.to_addrs, msg.cc_addrs, msg.subject, msg.body, msg.deck_id = new_to, new_cc, new_subject, new_body, new_deck
    msg.state = "edited"
    msg.error = None
    msg.gmail_draft_id = None
    _reset_approval(msg)
    msg.updated_at = stamp
    add_event(db, msg.id, "edited", member.id, "changed: " + ", ".join(changed), stamp, acted_by_id=acted_by_of(member))
    if was_approved:
        add_event(db, msg.id, "approval_reset", member.id, "edited after approval", stamp, acted_by_id=acted_by_of(member))
    db.commit()
    return msg


def approve(db: Session, msg_id: int, member: TeamMember, *, now: datetime | None = None) -> OutreachMessage:
    """Approve exactly the current content. Blocked by unfilled placeholders, missing/invalid
    recipients, an empty subject or body. Approving an approved message is a no-op."""
    msg = _load(db, msg_id)
    _require_owner(msg, member, "approve")
    if msg.state == "approved":
        return msg
    if msg.state not in ("draft", "edited"):
        raise DomainError("not_editable", f"A message that is {msg.state} cannot be approved.", 409)
    left = templates.find_placeholders(msg.subject) + templates.find_placeholders(msg.body)
    if left:
        raise DomainError(
            "unfilled_placeholders",
            f"Fill in the unfilled placeholder(s) before approving: {', '.join(dict.fromkeys(left))}.",
            409,
        )
    to = split_addrs(msg.to_addrs)
    if not to:
        raise DomainError("no_recipients", "Add at least one recipient before approving.", 409)
    for addr in to + split_addrs(msg.cc_addrs):
        try:
            validate_address(addr)
        except InvalidMessage as exc:
            raise DomainError("invalid_address", f"{exc}", 409) from exc
    suppressions.assert_not_suppressed(db, to, msg.cc_addrs)
    from sales0.services.crm import lifecycle

    lifecycle.ensure_contactable(db, company_id=msg.company_id, prospect_id=msg.prospect_id)
    if not msg.subject.strip() or not msg.body.strip():
        raise DomainError("empty_message", "The subject and body must not be empty.", 409)
    content = _build_content(db, msg)
    stamp = now_or(now)
    msg.content_sha256 = content.sha256
    msg.approved_by_member_id = member.id
    msg.approved_at = stamp
    msg.state = "approved"
    msg.error = None
    msg.updated_at = stamp
    add_event(
        db, msg.id, "approved", member.id, f"sha256={content.sha256[:12]}; {_attachment_note(db, msg)}", stamp,
        acted_by_id=acted_by_of(member),
    )  # fmt: skip
    db.commit()
    return msg


def reject(db: Session, msg_id: int, member: TeamMember, *, reason: str | None = None, now: datetime | None = None) -> OutreachMessage:
    msg = _load(db, msg_id)
    _require_owner(msg, member, "reject")
    if msg.state == "rejected":
        return msg
    if msg.state not in EDITABLE_STATES or (msg.state == "failed" and msg.send_outcome_unknown):
        raise DomainError("not_editable", f"A message that is {msg.state} cannot be rejected.", 409)
    stamp = now_or(now)
    msg.state = "rejected"
    _reset_approval(msg)
    msg.updated_at = stamp
    add_event(db, msg.id, "rejected", member.id, clip(reason, 300), stamp, acted_by_id=acted_by_of(member))
    db.commit()
    return msg


# ----------------------------------------------------------------------------- sending
def send(db: Session, msg_id: int, member: TeamMember, *, now: datetime | None = None) -> OutreachMessage:
    """Send an approved message (human click only). Idempotent for sending/sent.

    Also the way to retry a ``failed`` message whose outcome is KNOWN (nothing was sent) - that is
    still a deliberate click, never automatic. A failed send with an unknown outcome is refused
    here; use ``retry_send``.
    """
    msg = _load(db, msg_id)
    _require_owner(msg, member, "send")
    if msg.state in ("sending", "sent"):
        return msg
    if msg.state == "failed" and msg.send_outcome_unknown:
        raise DomainError(
            "outcome_unknown",
            "The last send attempt may or may not have gone out. Check your Gmail Sent folder, then use Retry and confirm it was not sent.",
            409,
        )
    if msg.state == "failed" and msg.content_sha256 is None:
        raise DomainError("not_approved", "This message must be approved again before it can be sent.", 409)
    if msg.state not in ("approved", "failed"):
        raise DomainError("not_approved", "Only an approved message can be sent - approve it first.", 409)
    prior = msg.state
    _assert_follow_up_still_wanted(db, msg)
    client, profile = _preflight(db, msg, member, now)
    claimed = _claim(db, msg, "(state = 'approved') OR (state = 'failed' AND send_outcome_unknown = FALSE AND content_sha256 IS NOT NULL)", now)
    if not claimed:
        return _load(db, msg_id)  # someone else claimed it first: report the current state
    add_event(db, msg.id, "sending", member.id, f"as {profile}", now, acted_by_id=acted_by_of(member))
    db.commit()
    _enforce_cap_after_claim(db, msg, member, prior, now)
    return _execute(db, msg, member, client, profile, now)


def retry_send(
    db: Session, msg_id: int, member: TeamMember, confirm_not_sent: bool, *, now: datetime | None = None
) -> OutreachMessage:
    """Re-send after a failure whose outcome was unknown. ``confirm_not_sent`` must be True: a
    human checked Gmail's Sent folder and the email is not there. Recorded in ``retry_decided_by``."""
    msg = _load(db, msg_id)
    _require_owner(msg, member, "retry")
    if msg.state in ("sending", "sent"):
        return msg
    if msg.state != "failed" or not msg.send_outcome_unknown:
        raise DomainError("not_retryable", "Only a send with an unknown outcome needs a confirmed retry.", 409)
    if not confirm_not_sent:
        raise DomainError(
            "confirmation_required",
            "Confirm that you checked Gmail's Sent folder and this email was NOT sent.",
            409,
        )
    if msg.content_sha256 is None:
        raise DomainError("not_approved", "This message must be approved again before it can be sent.", 409)
    _assert_follow_up_still_wanted(db, msg)
    client, profile = _preflight(db, msg, member, now)
    old_draft = msg.gmail_draft_id
    claimed = _claim(
        db, msg, "state = 'failed' AND send_outcome_unknown = TRUE AND content_sha256 IS NOT NULL", now,
        retry_decided_by=member.email[:80],
    )  # fmt: skip
    if not claimed:
        return _load(db, msg_id)
    add_event(db, msg.id, "retry_confirmed", member.id, f"confirmed not sent; as {profile}", now, acted_by_id=acted_by_of(member))
    db.commit()
    if old_draft:
        try:
            client.delete_draft(old_draft)  # best effort: never leave two live drafts of one message
        except Exception:  # noqa: BLE001 - already gone / sent / unreachable: nothing more to do
            pass
    _enforce_cap_after_claim(db, msg, member, "failed", now, unknown=True)
    return _execute(db, msg, member, client, profile, now)


def _preflight(db: Session, msg: OutreachMessage, member: TeamMember, now: datetime | None) -> tuple[GmailClient, str]:
    """Checks that need no state change: active member, usable connection, the Gmail account is the sender, cap."""
    if not member.active:
        raise DomainError("member_inactive", "This team member is deactivated and cannot send.", 403)
    suppressions.assert_not_suppressed(db, msg.to_addrs, msg.cc_addrs)  # final guard, before any Gmail call
    from sales0.services.crm import lifecycle

    lifecycle.ensure_contactable(db, company_id=msg.company_id, prospect_id=msg.prospect_id)  # Do not contact: never sent
    view = connections.connection_status(db, member.id)
    if not view.usable:
        raise DomainError(
            "gmail_not_connected",
            f"Gmail is not ready for {member.name} ({view.label}). Open Team and press Connect my Gmail.",
            409,
        )
    try:
        client = connections.client_for_member(member.id)
        profile = normalise_email(client.profile_email())
    except GmailError as exc:
        connections.record_failure(member.id, exc)
        raise DomainError("gmail_unavailable", f"Gmail could not be reached for {member.name}: {exc}", 409) from exc
    if profile != normalise_email(member.email) or profile != normalise_email(msg.from_addr):
        raise DomainError(
            "sender_mismatch",
            f"This Gmail connection is {profile}, but the message is from {msg.from_addr} ({member.name}). "
            "Nothing was sent - reconnect Gmail with the right account on the Team page.",
            409,
        )
    cap = get_settings().gmail_daily_send_cap
    if member_sends_today(db, member.id, now=now) >= cap:
        raise DomainError("daily_cap_reached", f"{member.name} has reached today's limit of {cap} emails. Try again tomorrow.", 429)
    return client, profile


def _claim(db: Session, msg: OutreachMessage, where_sql: str, now: datetime | None, **extra: object) -> bool:
    """Compare-and-swap into ``sending``. True when THIS call won; commits so others see it."""
    from sqlalchemy import text

    stamp = now_or(now)
    res = db.execute(
        update(OutreachMessage)
        .where(OutreachMessage.id == msg.id, text(f"({where_sql})"))
        .values(state="sending", updated_at=stamp, error=None, send_outcome_unknown=False, **extra)
        .execution_options(synchronize_session=False)
    )
    won = res.rowcount == 1
    db.commit()
    db.refresh(msg)
    return won


def _enforce_cap_after_claim(
    db: Session, msg: OutreachMessage, member: TeamMember, prior: str, now: datetime | None, unknown: bool = False
) -> None:
    """Race guard: count again with this message included; over the cap -> put it back untouched."""
    cap = get_settings().gmail_daily_send_cap
    if member_sends_today(db, member.id, now=now) <= cap:
        return
    db.execute(
        update(OutreachMessage)
        .where(OutreachMessage.id == msg.id, OutreachMessage.state == "sending")
        .values(state=prior, send_outcome_unknown=unknown, updated_at=now_or(now))
        .execution_options(synchronize_session=False)
    )
    add_event(db, msg.id, "send_blocked", member.id, f"daily cap {cap} reached", now, acted_by_id=acted_by_of(member))
    db.commit()
    raise DomainError("daily_cap_reached", f"{member.name} has reached today's limit of {cap} emails. Try again tomorrow.", 429)


def _fail(db: Session, msg: OutreachMessage, member: TeamMember, text: str, *, unknown: bool, now: datetime | None, clear_approval: bool = False) -> OutreachMessage:
    stamp = now_or(now)
    msg.state = "failed"
    msg.error = clip(text, 500)
    msg.send_outcome_unknown = unknown
    msg.updated_at = stamp
    if clear_approval:
        _reset_approval(msg)
    add_event(db, msg.id, "send_failed", member.id, ("outcome unknown: " if unknown else "not sent: ") + (clip(text, 400) or ""), stamp, acted_by_id=acted_by_of(member))
    db.commit()
    log.warning("send_failed", message_id=msg.id, member_id=member.id, outcome_unknown=unknown)
    return msg


def _thread_for(db: Session, msg: OutreachMessage, client: GmailClient) -> tuple[str | None, dict[str, str] | None]:
    """A follow-up joins the Gmail thread of the email it follows up (same mailbox only): its thread id plus the
    In-Reply-To / References headers of the newest message in it. Best effort - any problem sends it as a plain new
    email (the human already approved those words)."""
    if msg.follow_up_of_id is None:
        return None, None
    parent = db.get(OutreachMessage, msg.follow_up_of_id)
    if parent is None or not parent.gmail_thread_id or parent.member_id != msg.member_id:
        return None, None
    try:
        thread = client.get_thread(parent.gmail_thread_id)
    except Exception:  # noqa: BLE001 - unreachable / scope missing / gone: no threading, still sendable
        return None, None
    last = next((m for m in reversed(thread) if m.message_id_header), None)
    if last is None:
        return parent.gmail_thread_id, None
    refs = " ".join(x for x in (last.references, last.message_id_header) if x).strip()
    return parent.gmail_thread_id, {"In-Reply-To": last.message_id_header, "References": refs}


def _assert_follow_up_still_wanted(db: Session, msg: OutreachMessage) -> None:
    """A drafted follow-up email is only sent while its CRM reminder is still open: a reply, closing the prospect, a Do
    not follow up flag or doing it by hand cancels/finishes the reminder and the draft must not go out."""
    if msg.follow_up_id is None:
        return
    from sales0.db.models import FollowUp

    fu = db.get(FollowUp, msg.follow_up_id)
    if fu is None or fu.cancelled_at is not None:
        why = (fu.cancel_reason or "cancelled").replace("_", " ") if fu is not None else "removed"
        raise DomainError(
            "follow_up_stopped",
            f"This follow-up was stopped ({why}), so the email must not go out. Reject this draft.", 409,
        )
    if fu.done_at is not None:
        raise DomainError(
            "follow_up_stopped",
            "This follow-up was already marked done (by hand or by another email). Reject this draft if it is not needed.", 409,
        )


def _dispatch(client: GmailClient, approved: ApprovedMessage) -> tuple[str, str]:
    """The single call site of ``send_draft``: it needs an ApprovedMessage bound to the content hash."""
    return client.send_draft(approved)


def _delete_draft_quietly(client: GmailClient, draft_id: str | None) -> None:
    if not draft_id:
        return
    try:
        client.delete_draft(draft_id)
    except Exception:  # noqa: BLE001 - best effort cleanup
        pass


def _execute(
    db: Session, msg: OutreachMessage, member: TeamMember, client: GmailClient, profile: str, now: datetime | None
) -> OutreachMessage:
    """Run a claimed (state==sending) message through Gmail and record the outcome. Never raises for Gmail errors."""
    assert msg.content_sha256 is not None
    try:
        content = _build_content(db, msg)
    except DomainError as exc:
        return _fail(db, msg, member, exc.message, unknown=False, now=now)
    if content.sha256 != msg.content_sha256:
        return _fail(
            db, msg, member, "The message no longer matches what was approved (deck or content changed). Nothing was sent - approve it again.",
            unknown=False, now=now, clear_approval=True,
        )  # fmt: skip

    thread_id, thread_headers = _thread_for(db, msg, client)
    try:
        extra = {"thread_id": thread_id, "headers": thread_headers} if thread_id or thread_headers else {}
        draft_id = client.create_draft(
            list(content.to), list(content.cc), content.subject, content.text_body, None,
            [(a.filename, a.data, a.mime) for a in content.attachments], **extra,
        )  # fmt: skip
    except (GmailError, InvalidMessage) as exc:
        connections.record_failure(member.id, exc)
        return _fail(db, msg, member, f"Gmail draft could not be created: {exc}", unknown=False, now=now)
    msg.gmail_draft_id = draft_id
    db.commit()

    approver = db.get(TeamMember, msg.approved_by_member_id) if msg.approved_by_member_id else member
    approved = ApprovedMessage(
        draft_id=draft_id,
        sha256=msg.content_sha256,
        approved_by=(approver or member).email,
        approved_at=now_or(msg.approved_at),
    )
    try:
        message_id, thread_id = _dispatch(client, approved)
    except ApprovalMismatch as exc:
        _delete_draft_quietly(client, draft_id)
        msg.gmail_draft_id = None
        return _fail(db, msg, member, f"{exc}", unknown=False, now=now, clear_approval=True)
    except (GmailAuthError, GmailNotFound, GmailNotConfigured, GmailApiError, InvalidMessage) as exc:
        # Rejected with a definite 4xx before anything was sent (bad/expired credentials, draft missing, bad request).
        connections.record_failure(member.id, exc)
        _delete_draft_quietly(client, draft_id)
        msg.gmail_draft_id = None
        return _fail(db, msg, member, f"Gmail refused the send: {exc}", unknown=False, now=now)
    except Exception as exc:  # noqa: BLE001 - transient/network/unknown during the send itself: it may have gone out
        return _fail(
            db, msg, member,
            f"The send may or may not have completed ({type(exc).__name__}: {exc}). Check Gmail's Sent folder before retrying.",
            unknown=True, now=now,
        )  # fmt: skip

    stamp = now_or(now)
    msg.gmail_message_id, msg.gmail_thread_id = message_id, thread_id
    msg.state = "sent"
    msg.sent_at = stamp
    msg.error = None
    msg.send_outcome_unknown = False
    msg.updated_at = stamp
    add_event(
        db, msg.id, "sent", member.id, f"from {profile}; gmail_message={message_id}; {_attachment_note(db, msg)}", stamp,
        acted_by_id=acted_by_of(member),
    )  # fmt: skip
    events.publish(
        db,
        events.MessageSent(
            outreach_message_id=msg.id,
            member_id=member.id,
            to_addrs=tuple(split_addrs(msg.to_addrs)),
            sent_at=stamp,
        ),
    )
    db.commit()
    log.info("message_sent", message_id=msg.id, member_id=member.id)
    return msg


__all__ = [
    "UNSET",
    "approve",
    "get_message",
    "list_events",
    "list_outbox",
    "member_sends_today",
    "reject",
    "retry_send",
    "send",
    "update_message",
]
