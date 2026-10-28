"""Reply tracking: poll each member's own mailbox for replies to their SENT outreach.

READ-ONLY on the mailbox: only ``get_thread`` / ``list_threads_with_new_messages`` are called;
nothing is ever marked read, archived, labelled or sent. Effects on sales0 data go through the
domain event ``ReplyReceived`` (see ``services.events``); the default subscriber here sets
``outreach_messages.reply_status``, stops pending follow-up drafts and fills the suppression list.

Idempotent: ``outreach_replies.gmail_message_id`` is UNIQUE and already-stored ids are skipped
before any classification (so a second poll makes no LLM calls and no duplicate rows).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc
from sales0.config import Settings, get_settings
from sales0.db.engine import session_scope
from sales0.db.models import OutreachMessage, OutreachReply, ReplyPollState, TeamMember
from sales0.errors import DomainError
from sales0.integrations.gmail.client import GmailClient
from sales0.integrations.gmail.errors import GmailAuthError, GmailError, GmailNotFound
from sales0.integrations.gmail.threads import ThreadMessage
from sales0.llm import LLMBadResponse, LLMClient, LLMError
from sales0.logging import get_logger
from sales0.services import events, reply_classify, suppressions
from sales0.services._common import clip, not_found, now_or
from sales0.services.events import ReplyReceived
from sales0.services.outreach._shared import add_event, split_addrs
from sales0.services.team import connections
from sales0.services.team.members import normalise_email

log = get_logger("sales0.replies")

PENDING_FOLLOWUP_STATES = ("draft", "edited", "approved")
STATUS_RANK = {"none": 0, "auto_reply": 1, "bounced": 2, "replied": 3}
BACKOFF_CAP = timedelta(hours=6)
LISTING_OVERLAP = timedelta(hours=1)


@dataclass
class PollResult:
    member_id: int
    threads_checked: int = 0
    new_replies: int = 0
    skipped_deferred: int = 0  # LLM unavailable: retried next cycle
    status: str = "ok"  # ok | skipped | error
    error: str | None = None
    kinds: dict[str, int] = field(default_factory=dict)


# ----------------------------------------------------------------------------- own addresses
def own_addresses(db: Session, msg: OutreachMessage, settings: Settings | None = None) -> set[str]:
    """Addresses whose messages are never replies: the sender, the connection, configured aliases
    and every team member (a colleague answering in the thread is not the prospect)."""
    settings = settings or get_settings()
    own = {normalise_email(msg.from_addr), normalise_email(msg.connection_email)}
    own |= {normalise_email(a) for a in settings.reply_own_aliases.replace(";", ",").split(",") if a.strip()}
    own |= set(db.execute(select(TeamMember.email)).scalars())
    return {a for a in own if a}


def inbound_messages(thread: list[ThreadMessage], msg: OutreachMessage, own: set[str]) -> list[ThreadMessage]:
    out = []
    for m in thread:
        if m.id == msg.gmail_message_id or m.is_sent or m.is_draft:
            continue
        if not m.from_addr or m.from_addr in own:
            continue
        if msg.sent_at is not None and m.date < ensure_utc(msg.sent_at) - timedelta(minutes=1):
            continue
        out.append(m)
    return out


# ----------------------------------------------------------------------------- ingest
def _failed_recipients(m: ThreadMessage, msg: OutreachMessage) -> tuple[str, ...]:
    """Who a bounce is about: X-Failed-Recipients when present, else the original recipients."""
    named = [a.lower() for a in split_addrs(m.x_failed_recipients)]
    return tuple(named or [a.lower() for a in split_addrs(msg.to_addrs)])


def ingest_thread(
    db: Session, msg: OutreachMessage, thread: list[ThreadMessage], llm: LLMClient | None, result: PollResult,
    *, settings: Settings | None = None, now: datetime | None = None,
) -> None:
    """Store + classify + publish each not-yet-seen inbound message of ``thread`` for ``msg``."""
    ensure_default_subscribers()
    own = own_addresses(db, msg, settings)
    candidates = inbound_messages(thread, msg, own)
    if not candidates:
        return
    seen = set(db.execute(select(OutreachReply.gmail_message_id).where(OutreachReply.gmail_message_id.in_([m.id for m in candidates]))).scalars())
    stamp = now_or(now)
    for m in candidates:
        if m.id in seen:
            continue
        verdict = reply_classify.classify_by_headers(m)
        if verdict is None:
            try:
                verdict = reply_classify.classify_with_llm(db, llm, m, now=stamp)
            except reply_classify.FATAL_LLM_ERRORS:
                raise
            except LLMBadResponse:
                verdict = reply_classify.Classification("human", "other", "AI answer unusable; shown for a human to read", "fallback")
            except LLMError as exc:
                result.skipped_deferred += 1
                result.error = f"AI classifier unavailable ({type(exc).__name__}); will retry next cycle"
                continue
        reply = OutreachReply(
            message_id=msg.id, member_id=msg.member_id, gmail_message_id=m.id, gmail_thread_id=m.thread_id or (msg.gmail_thread_id or ""),
            from_addr=m.from_addr[:254], received_at=m.date, snippet=(clip(m.snippet, 500) or ""), kind=verdict.kind,
            intent=verdict.intent, reason=clip(verdict.reason, 300), classified_by=verdict.by, handled=False, created_at=stamp,
        )  # fmt: skip
        db.add(reply)
        db.flush()
        add_event(db, msg.id, "reply_received", None, f"kind={verdict.kind} intent={verdict.intent or '-'} by={verdict.by}", stamp)
        result.new_replies += 1
        result.kinds[verdict.kind] = result.kinds.get(verdict.kind, 0) + 1
        events.publish(
            db,
            ReplyReceived(
                outreach_message_id=msg.id, kind=verdict.kind, intent=verdict.intent, reply_id=reply.id, member_id=msg.member_id,
                from_addr=m.from_addr, hard_bounce=verdict.hard_bounce,
                failed_recipients=_failed_recipients(m, msg) if verdict.kind == "bounce" else (),
            ),
        )
    db.flush()


# ----------------------------------------------------------------------------- default subscriber
def on_reply_received(db: Session, ev: ReplyReceived) -> None:
    """Default effects: reply_status, stop pending follow-ups, suppress unsubscribes and hard bounces."""
    msg = db.get(OutreachMessage, ev.outreach_message_id)
    if msg is None:
        return
    new_status = {"human": "replied", "bounce": "bounced", "auto_reply": "auto_reply"}[ev.kind]
    if STATUS_RANK[new_status] > STATUS_RANK.get(msg.reply_status, 0):
        msg.reply_status = new_status
    if ev.kind != "auto_reply":
        msg.last_reply_at = now_or(None)
        _stop_follow_ups(db, msg, ev)
    if ev.kind == "human" and ev.intent == "unsubscribe":
        for addr in {ev.from_addr, *[a.lower() for a in split_addrs(msg.to_addrs)]}:
            suppressions.add(db, addr, "unsubscribe", source_reply_id=ev.reply_id, note="asked to stop (reply)")
    if ev.kind == "bounce" and ev.hard_bounce:
        for addr in ev.failed_recipients:
            suppressions.add(db, addr, "bounce", source_reply_id=ev.reply_id, note="delivery failed")
    db.flush()


def _stop_follow_ups(db: Session, msg: OutreachMessage, ev: ReplyReceived) -> None:
    rows = db.execute(
        select(OutreachMessage).where(
            OutreachMessage.follow_up_of_id == msg.id, OutreachMessage.state.in_(PENDING_FOLLOWUP_STATES)
        )
    ).scalars()
    why = "a reply was received" if ev.kind == "human" else "the original email bounced"
    for fu in rows:
        fu.state = "rejected"
        fu.approved_at = None
        fu.approved_by_member_id = None
        fu.content_sha256 = None
        fu.error = f"Follow-up stopped: {why}."
        add_event(db, fu.id, "follow_up_stopped", None, why)


def ensure_default_subscribers() -> None:
    events.subscribe(ReplyReceived, on_reply_received)


# ----------------------------------------------------------------------------- polling
def _state(db: Session, member_id: int) -> ReplyPollState:
    row = db.execute(select(ReplyPollState).where(ReplyPollState.member_id == member_id)).scalar_one_or_none()
    if row is None:
        row = ReplyPollState(member_id=member_id)
        db.add(row)
        db.flush()
    return row


def sent_candidates(db: Session, member_id: int, now: datetime, settings: Settings) -> list[OutreachMessage]:
    since = now - timedelta(days=settings.reply_lookback_days)
    return list(
        db.execute(
            select(OutreachMessage)
            .where(
                OutreachMessage.member_id == member_id, OutreachMessage.state == "sent",
                OutreachMessage.gmail_thread_id.is_not(None), OutreachMessage.sent_at >= since,
            )
            .order_by(OutreachMessage.sent_at.desc())
            .limit(settings.reply_max_threads_per_poll)
        ).scalars()
    )  # fmt: skip


def poll_member(
    member_id: int, *, llm: LLMClient | None = None, now: datetime | None = None, settings: Settings | None = None,
    force: bool = False,
) -> PollResult:
    """One poll cycle for one member. Never raises on Gmail/LLM trouble: it is recorded in
    ``reply_poll_state`` (with exponential backoff) and returned in the result."""
    settings = settings or get_settings()
    stamp = now_or(now)
    result = PollResult(member_id)
    with session_scope() as db:
        state = _state(db, member_id)
        if not force and state.next_attempt_at is not None and ensure_utc(state.next_attempt_at) > stamp:
            result.status = "skipped"
            return result
        view = connections.connection_status(db, member_id)
        if not view.usable:
            state.last_polled_at = stamp
            state.last_error = f"Gmail not ready ({view.label}); reconnect on the Team page"
            result.status, result.error = "skipped", state.last_error
            return result
        candidates = sent_candidates(db, member_id, stamp, settings)
        last_ok = ensure_utc(state.last_ok_at) if state.last_ok_at else None
        cand_ids = [c.id for c in candidates]
    if not cand_ids:
        _record_ok(member_id, stamp, result)
        return result
    try:
        client = connections.client_for_member(member_id)
        connections.verify_client_identity(member_id, client)
        wanted = _threads_to_fetch(client, candidates, last_ok, settings)
        for mid in cand_ids:
            with session_scope() as db:
                msg = db.get(OutreachMessage, mid)
                if msg is None or msg.gmail_thread_id not in wanted:
                    continue
                try:
                    thread = client.get_thread(msg.gmail_thread_id or "")
                except GmailNotFound:
                    continue  # thread deleted by the user: nothing to track
                result.threads_checked += 1
                ingest_thread(db, msg, thread, llm, result, settings=settings, now=stamp)
    except GmailAuthError as exc:
        connections.record_failure(member_id, exc)
        _record_failure(member_id, stamp, result, f"Gmail rejected the credentials: {exc}", settings)
        return result
    except GmailError as exc:
        _record_failure(member_id, stamp, result, f"Gmail error: {exc}", settings)
        return result
    except reply_classify.FATAL_LLM_ERRORS as exc:
        _record_failure(member_id, stamp, result, f"AI classifier unavailable: {exc}", settings)
        return result
    _record_ok(member_id, stamp, result)
    log.info("reply_poll_done", member_id=member_id, threads=result.threads_checked, new=result.new_replies, deferred=result.skipped_deferred)
    return result


def _threads_to_fetch(client: GmailClient, candidates: list[OutreachMessage], last_ok: datetime | None, settings: Settings) -> set[str]:
    """Thread ids worth fetching. After a successful cycle only threads Gmail says changed since
    (with an hour of overlap for index lag); a truncated or failed listing falls back to all."""
    every = {c.gmail_thread_id or "" for c in candidates}
    if last_ok is None:
        return every
    limit = settings.reply_max_threads_per_poll
    try:
        changed = client.list_threads_with_new_messages(last_ok - LISTING_OVERLAP, limit=limit)
    except GmailAuthError:
        raise
    except GmailError:
        return every
    if len(changed) >= limit:
        return every
    return every & set(changed)


def _record_ok(member_id: int, stamp: datetime, result: PollResult) -> None:
    with session_scope() as db:
        st = _state(db, member_id)
        st.last_polled_at = stamp
        st.last_new_replies = result.new_replies
        if result.skipped_deferred:  # keep last_ok so the listing window still covers deferred threads
            st.last_error = result.error
        else:
            st.last_ok_at = stamp
            st.last_error = None
        st.consecutive_failures = 0
        st.next_attempt_at = None


def _record_failure(member_id: int, stamp: datetime, result: PollResult, error: str, settings: Settings) -> None:
    result.status, result.error = "error", error[:300]
    base = timedelta(minutes=max(settings.reply_poll_minutes, 1))
    with session_scope() as db:
        st = _state(db, member_id)
        st.consecutive_failures += 1
        st.last_polled_at = stamp
        st.last_error = error[:300]
        st.next_attempt_at = stamp + min(base * (2 ** min(st.consecutive_failures, 10)), BACKOFF_CAP)
        failures = st.consecutive_failures
    log.warning("reply_poll_failed", member_id=member_id, failures=failures)


def poll_all(*, llm: LLMClient | None = None, now: datetime | None = None, settings: Settings | None = None, force: bool = False) -> list[PollResult]:
    """Poll every active member who has sent mail. One member's failure never stops the others."""
    settings = settings or get_settings()
    ensure_default_subscribers()
    with session_scope() as db:
        ids = list(
            db.execute(
                select(OutreachMessage.member_id).join(TeamMember, TeamMember.id == OutreachMessage.member_id)
                .where(OutreachMessage.state == "sent", TeamMember.active.is_(True)).distinct()
            ).scalars()
        )  # fmt: skip
    results = []
    for mid in sorted(ids):
        try:
            results.append(poll_member(mid, llm=llm, now=now, settings=settings, force=force))
        except Exception:  # noqa: BLE001 - never let one member kill the cycle
            log.exception("reply_poll_member_crashed", member_id=mid)
    return results


# ----------------------------------------------------------------------------- queries / actions
@dataclass(frozen=True)
class ReplyView:
    reply: OutreachReply
    message: OutreachMessage
    owner: str


def list_replies(
    db: Session, member: TeamMember | None = None, *, kind: str | None = None, intent: str | None = None,
    handled: bool | None = None, everyone: bool = False, limit: int = 200,
) -> list[ReplyView]:
    stmt = (
        select(OutreachReply, OutreachMessage, TeamMember.name)
        .join(OutreachMessage, OutreachMessage.id == OutreachReply.message_id)
        .join(TeamMember, TeamMember.id == OutreachReply.member_id)
        .order_by(OutreachReply.received_at.desc(), OutreachReply.id.desc())
        .limit(limit)
    )
    if member is not None and not (everyone and member.role == "admin"):
        stmt = stmt.where(OutreachReply.member_id == member.id)
    if kind:
        stmt = stmt.where(OutreachReply.kind == kind)
    if intent:
        stmt = stmt.where(OutreachReply.intent == intent)
    if handled is not None:
        stmt = stmt.where(OutreachReply.handled.is_(handled))
    return [ReplyView(r, m, name) for r, m, name in db.execute(stmt).all()]


def unhandled_human_count(db: Session, member: TeamMember | None = None) -> int:
    stmt = select(func.count()).select_from(OutreachReply).where(OutreachReply.kind == "human", OutreachReply.handled.is_(False))
    if member is not None and member.role != "admin":
        stmt = stmt.where(OutreachReply.member_id == member.id)
    return int(db.execute(stmt).scalar_one())


def reply_counts_by_message(db: Session, message_ids: list[int]) -> dict[int, dict[str, int]]:
    """{message_id: {kind: count}} for the outbox badges."""
    if not message_ids:
        return {}
    rows = db.execute(
        select(OutreachReply.message_id, OutreachReply.kind, func.count())
        .where(OutreachReply.message_id.in_(message_ids)).group_by(OutreachReply.message_id, OutreachReply.kind)
    ).all()  # fmt: skip
    out: dict[int, dict[str, int]] = {}
    for mid, kind, n in rows:
        out.setdefault(mid, {})[kind] = int(n)
    return out


def set_handled(db: Session, reply_id: int, member: TeamMember, handled: bool = True, *, now: datetime | None = None) -> OutreachReply:
    row = db.get(OutreachReply, reply_id)
    if row is None or (member.role != "admin" and row.member_id != member.id):
        raise not_found()
    row.handled = handled
    row.handled_by_member_id = member.id if handled else None
    row.handled_at = now_or(now) if handled else None
    db.flush()
    return row


def mark_all_handled(db: Session, member: TeamMember, reply_ids: list[int]) -> int:
    n = 0
    for rid in reply_ids:
        try:
            set_handled(db, rid, member, True)
            n += 1
        except DomainError:
            continue
    return n


# ----------------------------------------------------------------------------- health (header status line)
@dataclass(frozen=True)
class PollerHealth:
    enabled: bool
    level: str  # off | idle | ok | warn
    label: str
    detail: str


def poller_health(db: Session, settings: Settings | None = None, now: datetime | None = None) -> PollerHealth:
    settings = settings or get_settings()
    if settings.reply_poll_minutes <= 0:
        return PollerHealth(False, "off", "Replies: off", "Reply polling is off (SALES0_REPLY_POLL_MINUTES=0).")
    states = list(db.execute(select(ReplyPollState)).scalars())
    names = {m.id: m.name for m in db.execute(select(TeamMember)).scalars()}
    bad = [s for s in states if s.consecutive_failures > 0 or (s.last_error and s.last_ok_at is None)]
    if bad:
        who = ", ".join(sorted(names.get(s.member_id, "?") for s in bad))
        return PollerHealth(True, "warn", f"Replies: problem ({who})", "; ".join(f"{names.get(s.member_id, '?')}: {s.last_error}" for s in bad)[:300])
    oks = [ensure_utc(s.last_ok_at) for s in states if s.last_ok_at]
    unready = [s for s in states if s.last_error]
    if unready:
        who = ", ".join(sorted(names.get(s.member_id, "?") for s in unready))
        return PollerHealth(True, "warn", f"Replies: needs attention ({who})", "; ".join(f"{names.get(s.member_id, '?')}: {s.last_error}" for s in unready)[:300])
    if not oks:
        return PollerHealth(True, "idle", "Replies: waiting for first check", f"Checks every {settings.reply_poll_minutes} min.")
    from sales0.web.templating import ago

    return PollerHealth(True, "ok", f"Replies: checked {ago(max(oks))}", f"Checks every {settings.reply_poll_minutes} min.")


__all__ = [
    "PollResult", "PollerHealth", "ReplyView", "ensure_default_subscribers", "ingest_thread", "list_replies",
    "on_reply_received", "poll_all", "poll_member", "poller_health", "set_handled",
]  # fmt: skip
