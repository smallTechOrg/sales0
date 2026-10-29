"""AI-drafted follow-up EMAILS for due CRM reminders (feedback round 2, E2) - the safe version.

What is automated: when an email follow-up (Follow-up 1 / 2 / Final, planned channel Email) is due, Haiku
(``claude-haiku-5-5``) WRITES a draft - using the prospect's newest research, account summary and the earlier emails of the
thread - and parks it in the outbox as an ordinary ``draft`` message.

What is never automated: approving and sending. The draft goes through exactly the same state machine as every email
(``messages.approve`` -> ``messages.send``, a human click, bound to the content hash); this module never imports either.
Sending marks THAT reminder done (channel email) and moves the stage through the existing CRM hook.

Skipped, with a visible reason: not an Email-planned reminder, prospect closed / converted / "Do not follow up",
a reply (or bounce) already arrived, the recipient is suppressed or there is none, a draft already exists, the thread
belongs to another team member.

Request-bound by design (hosted mode has no scheduler): ``draft_due`` does at most ``limit`` AI calls per click (and stops
at the hosted action budget) and reports how many are left, so the person clicks again. The local scheduler may call it
once a day through the opt-in ``followup_drafts`` job (default off).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from sales0.clock import local_today
from sales0.config import get_settings
from sales0.db.models import Contact, Enquiry, Flag, FollowUp, OutreachMessage, OutreachReply, Prospect, TeamMember
from sales0.errors import DomainError
from sales0.llm import LLMAuthError, LLMClient, LLMModelNotFound
from sales0.logging import get_logger
from sales0.services import suppressions
from sales0.services._common import clip, now_or, not_found
from sales0.services.costs_report import SpendCapReached
from sales0.services.crm import followups
from sales0.services.crm.views import FollowUpView
from sales0.services.outreach import drafting
from sales0.services.outreach._shared import split_addrs

log = get_logger("sales0.followup_drafts")

DRAFTABLE_KINDS = followups.AUTO_KINDS
PENDING_STATES = ("draft", "edited", "approved", "sending", "failed")
THREAD_MESSAGES = 3  # the last N sent emails are shown to the model
THREAD_BODY_CHARS = 1500
SEQUENCE = {"follow_up_1": 1, "follow_up_2": 2, "final_follow_up": 3}
MAX_SCAN = 300  # due reminders looked at per click (bounded work)


@dataclass(frozen=True)
class Eligibility:
    ok: bool
    reason: str | None = None  # why not (shown to the person)
    message_id: int | None = None  # an existing draft when the reason is "already drafted"
    to: str = ""
    cc: str = ""
    parent_id: int | None = None
    owner_id: int | None = None  # the team member whose mailbox sends it


@dataclass
class DraftOutcome:
    follow_up_id: int
    prospect_id: int | None
    prospect_name: str
    kind_label: str
    status: Literal["drafted", "skipped", "failed"]
    reason: str | None = None
    message_id: int | None = None


@dataclass
class BatchResult:
    outcomes: list[DraftOutcome] = field(default_factory=list)
    remaining: int = 0  # eligible reminders not drafted yet (cap / time budget): click again
    stopped: str | None = None  # why the batch stopped early (spend cap, AI down, time budget)

    @property
    def drafted(self) -> int:
        return sum(o.status == "drafted" for o in self.outcomes)

    @property
    def skipped(self) -> int:
        return sum(o.status == "skipped" for o in self.outcomes)

    @property
    def failed(self) -> int:
        return sum(o.status == "failed" for o in self.outcomes)

    def summary(self) -> str:
        parts = [f"Drafted {self.drafted} follow-up email{'' if self.drafted == 1 else 's'} for you to review"]
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.failed:
            parts.append(f"{self.failed} failed")
        text = ", ".join(parts) + "."
        if self.remaining:
            text += f" {self.remaining} more due - click again."
        if self.stopped:
            text += f" Stopped: {self.stopped}"
        return text[:290]


# ---------------------------------------------------------------------------- eligibility
def _today(now: datetime | None = None) -> date:
    return local_today(get_settings().tz, now)


def _prospect_messages(db: Session, p: Prospect):  # type: ignore[no-untyped-def]
    enq = select(Enquiry.id).where(Enquiry.prospect_id == p.id)
    return or_(OutreachMessage.prospect_id == p.id, OutreachMessage.enquiry_id.in_(enq))


def reply_seen(db: Session, p: Prospect) -> str | None:
    """A reason when this prospect has already answered (or the address bounced): follow-ups stop there."""
    if p.responded_at is not None:
        return "they already replied - follow-ups stop once a reply arrives"
    hit = db.scalar(
        select(OutreachReply.kind).join(OutreachMessage, OutreachMessage.id == OutreachReply.message_id)
        .where(_prospect_messages(db, p), OutreachReply.kind.in_(("human", "bounce"))).order_by(OutreachReply.id.desc()).limit(1)
    )
    if hit == "human":
        return "they already replied - follow-ups stop once a reply arrives"
    if hit == "bounce":
        return "the last email bounced (delivery failed)"
    return None


def _latest_sent(db: Session, p: Prospect) -> OutreachMessage | None:
    return db.scalar(
        select(OutreachMessage).where(_prospect_messages(db, p), OutreachMessage.state == "sent")
        .order_by(OutreachMessage.sent_at.desc(), OutreachMessage.id.desc()).limit(1)
    )


def _primary_email(db: Session, p: Prospect) -> str:
    if p.contact_id:
        c = db.get(Contact, p.contact_id)
        if c is not None and c.email:
            return c.email
    return ""


def check(db: Session, f: FollowUp, member: TeamMember | None, *, today: date | None = None, require_due: bool = True) -> Eligibility:
    """Can an email draft be written for this reminder right now? Pure reads."""
    today = today or _today()
    if f.done_at is not None or f.cancelled_at is not None:
        return Eligibility(False, "this follow-up is already finished")
    if f.kind not in DRAFTABLE_KINDS or f.prospect_id is None:
        return Eligibility(False, "only Follow-up 1, 2 and the Final follow-up of a prospect are drafted")
    if (f.planned_channel or "email") != "email":
        return Eligibility(False, f"planned for {followups.CHANNEL_LABELS.get(f.planned_channel or '', f.planned_channel)}, not email")
    if require_due and f.due_on > today:
        return Eligibility(False, f"not due until {f.due_on.isoformat()}")
    existing = db.scalar(
        select(OutreachMessage).where(OutreachMessage.follow_up_id == f.id, OutreachMessage.state.in_((*PENDING_STATES, "sent")))
        .order_by(OutreachMessage.id.desc()).limit(1)
    )
    if existing is not None:
        return Eligibility(False, "a draft already exists" if existing.state != "sent" else "its email was already sent", message_id=existing.id)
    earlier = db.scalar(
        select(FollowUp).where(
            FollowUp.prospect_id == f.prospect_id, FollowUp.id != f.id, FollowUp.done_at.is_(None), FollowUp.cancelled_at.is_(None),
            FollowUp.kind.in_(DRAFTABLE_KINDS), or_(FollowUp.due_on < f.due_on, (FollowUp.due_on == f.due_on) & (FollowUp.id < f.id)),
        ).order_by(FollowUp.due_on, FollowUp.id).limit(1)
    )
    if earlier is not None:  # one at a time, in order: the next reminder waits until the earlier one is done
        return Eligibility(False, f"{followups.KIND_LABELS[earlier.kind]} comes first")
    p = db.get(Prospect, f.prospect_id)
    if p is None:
        return Eligibility(False, "the prospect no longer exists")
    if p.status != "open":
        return Eligibility(False, f"the prospect is {p.status}" + (" (a lead now)" if p.status == "converted" else ""))
    if db.scalar(select(Flag.id).where(Flag.entity_type == "prospect", Flag.entity_id == p.id, Flag.kind == "no_follow_up",
                                       Flag.cleared_at.is_(None)).limit(1)):
        return Eligibility(False, "marked Do not follow up")
    gone = reply_seen(db, p)
    if gone:
        return Eligibility(False, gone)
    parent = _latest_sent(db, p)
    owner_id = parent.member_id if parent is not None else (member.id if member is not None else None)
    if owner_id is None:
        return Eligibility(False, "no earlier email from this app and no sender to write as")
    if member is not None and parent is not None and parent.member_id != member.id:
        owner = db.get(TeamMember, parent.member_id)
        return Eligibility(False, f"the thread belongs to {owner.name if owner else 'another team member'}, who drafts it")
    sender = db.get(TeamMember, owner_id)
    if sender is None or not sender.active:
        return Eligibility(False, "the sender is deactivated")
    to = parent.to_addrs if parent is not None else _primary_email(db, p)
    cc = parent.cc_addrs if parent is not None else ""
    if not split_addrs(to):
        return Eligibility(False, "no email address for this prospect")
    hit = suppressions.suppressed_among(db, to, cc)
    if hit:
        return Eligibility(False, f"{', '.join(hit)} is on the suppression list (unsubscribed or bounced)")
    return Eligibility(True, to=to, cc=cc, parent_id=parent.id if parent is not None else None, owner_id=owner_id)


def explain(db: Session, rows: list[FollowUp], member: TeamMember | None, *, today: date | None = None) -> dict[int, Eligibility]:
    """Eligibility of each OPEN reminder (the prospect page shows the button or the reason)."""
    return {f.id: check(db, f, member, today=today, require_due=False) for f in rows if f.done_at is None and f.cancelled_at is None}


# ---------------------------------------------------------------------------- context
def _strip_re(subject: str) -> str:
    s = (subject or "").strip()
    while s.lower().startswith(("re:", "fwd:", "fw:")):
        s = s.split(":", 1)[1].strip()
    return s


def _context(db: Session, p: Prospect, f: FollowUp, elig: Eligibility, today: date) -> tuple[drafting.FollowUpContext, dict]:
    sent = list(db.scalars(
        select(OutreachMessage).where(_prospect_messages(db, p), OutreachMessage.state == "sent")
        .order_by(OutreachMessage.sent_at.desc(), OutreachMessage.id.desc()).limit(THREAD_MESSAGES)
    ))[::-1]
    thread = tuple(
        drafting.ThreadItem(sent_text=m.sent_at.strftime("%d %b %Y") if m.sent_at else "", subject=m.subject, body=clip(m.body, THREAD_BODY_CHARS) or "")
        for m in sent
    )
    first = sent[0].sent_at if sent and sent[0].sent_at else p.contacted_at
    days = max((today - local_today(get_settings().tz, first)).days, 0) if first is not None else None
    parent = db.get(OutreachMessage, elig.parent_id) if elig.parent_id else None
    subject = f"Re: {_strip_re(parent.subject)}" if parent is not None and _strip_re(parent.subject) else None
    ctx = drafting.FollowUpContext(
        kind_label=followups.KIND_LABELS[f.kind], sequence=SEQUENCE[f.kind], days_since_first=days, thread=thread, subject=subject,
        follow_up_id=f.id, follow_up_of_id=parent.id if parent is not None else None,
    )
    enq = db.scalars(select(Enquiry).where(Enquiry.prospect_id == p.id).order_by(Enquiry.date_added.desc(), Enquiry.id.desc()).limit(1)).first()
    first_name = ""
    addr = (split_addrs(elig.to) or [""])[0].lower()
    contact = db.scalar(select(Contact).where(Contact.email == addr).limit(1)) if addr else None
    if contact is None and p.contact_id:
        contact = db.get(Contact, p.contact_id)
    if contact is not None and contact.full_name:
        first_name = contact.full_name.split()[0]
    from sales0.services.crm import prospects as prospects_service

    company = prospects_service.get_prospect(db, p.id).company_name or p.name
    return ctx, {
        "first_name": first_name or None, "company": company,
        "requirement": ((enq.requirement_summary or (enq.raw_text or "")[:500]) if enq is not None else "") or p.name,
        "enquiry_id": enq.id if enq is not None else None,
    }


# ---------------------------------------------------------------------------- drafting
def draft_one(
    db: Session, llm: LLMClient | None, f: FollowUp, member: TeamMember | None, *, now: datetime | None = None,
    require_due: bool = True, elig: Eligibility | None = None,
) -> OutreachMessage:
    """Write ONE follow-up email draft (an ordinary ``draft`` message owned by the thread's sender). Raises
    ``followup_not_draftable`` (with the reason) when it must be skipped. Flushed; the caller commits."""
    from sales0.services.research import prospect as prospect_research
    from sales0.services.research import summary as summary_service

    stamp = now_or(now)
    today = _today(stamp)
    elig = elig or check(db, f, member, today=today, require_due=require_due)
    if not elig.ok:
        raise DomainError("followup_not_draftable", f"No email drafted: {elig.reason}.", 409)
    p = db.get(Prospect, f.prospect_id)
    assert p is not None and elig.owner_id is not None
    sender = db.get(TeamMember, elig.owner_id)
    assert sender is not None
    ctx, base = _context(db, p, f, elig, today)
    brief, _doc = prospect_research.brief_for_drafting(db, p.id)
    msg = drafting.generate_draft(
        db, llm, sender, to=elig.to, cc=elig.cc or None, company=base["company"], requirement=base["requirement"], first_name=base["first_name"],
        brief=brief, account_summary=summary_service.summary_for_drafting(db, p.id), enquiry_id=base["enquiry_id"],
        prospect_id=p.id, company_id=p.company_id, follow_up=ctx, now=stamp,
    )
    log.info("followup_draft_written", follow_up_id=f.id, prospect_id=p.id, message_id=msg.id, member_id=sender.id)
    return msg


def draft_follow_up(db: Session, llm: LLMClient | None, follow_up_id: int, member: TeamMember, *, now: datetime | None = None) -> OutreachMessage:
    """The per-follow-up "Draft email" button (works for any open Email-planned reminder, due or not)."""
    f = db.get(FollowUp, follow_up_id)
    if f is None:
        raise not_found()
    msg = draft_one(db, llm, f, member, now=now, require_due=False)
    db.commit()
    return msg


def draft_due(
    db: Session, llm: LLMClient | None, member: TeamMember | None, *, limit: int | None = None, prospect_id: int | None = None,
    now: datetime | None = None, budget_s: float | None = None,
) -> BatchResult:
    """Draft every due, eligible Email follow-up - at most ``limit`` AI calls (default ``SALES0_FOLLOWUP_DRAFT_BATCH``) -
    and report the rest. ``member`` = the person clicking (drafts only threads they own; ``None`` = the local scheduler:
    each draft is owned by the thread's sender). Commits after every draft so a later failure loses nothing."""
    settings = get_settings()
    limit = limit or settings.followup_draft_batch
    if budget_s is None and settings.is_hosted:
        budget_s = float(settings.hosted_action_budget_s)
    started = time.monotonic()
    stamp = now_or(now)
    today = _today(stamp)
    panel = followups.list_due(db, today=today)
    rows = [v for v in (*panel.overdue, *panel.due_today) if v.kind in DRAFTABLE_KINDS and v.prospect_id is not None
            and (prospect_id is None or v.prospect_id == prospect_id)][:MAX_SCAN]
    result = BatchResult()
    consecutive_failures = 0
    for v in rows:
        f = db.get(FollowUp, v.id)
        if f is None:
            continue
        elig = check(db, f, member, today=today)
        if not elig.ok:
            result.outcomes.append(DraftOutcome(f.id, v.prospect_id, v.who, v.kind_label, "skipped", elig.reason, elig.message_id))
            continue
        if result.drafted >= limit or (budget_s is not None and time.monotonic() - started > budget_s):
            result.remaining += 1
            continue
        try:
            msg = draft_one(db, llm, f, member, now=stamp, elig=elig)
            db.commit()
            result.outcomes.append(DraftOutcome(f.id, v.prospect_id, v.who, v.kind_label, "drafted", None, msg.id))
            consecutive_failures = 0
        except SpendCapReached as exc:
            db.rollback()
            result.stopped = exc.message
            result.remaining += 1
            break
        except (LLMAuthError, LLMModelNotFound):
            db.commit()
            raise
        except DomainError as exc:
            db.rollback()
            result.outcomes.append(DraftOutcome(f.id, v.prospect_id, v.who, v.kind_label, "failed", exc.message, None))
            consecutive_failures += 1
            if consecutive_failures >= 2:
                result.stopped = "the AI service keeps failing - try again in a minute."
                break
    return result


def due_overview(db: Session, member: TeamMember | None, *, today: date | None = None) -> list[tuple[FollowUpView, Eligibility]]:
    """Every due Email-planned auto reminder with its eligibility (the outbox lists them: ready, or the reason)."""
    today = today or _today()
    panel = followups.list_due(db, today=today)
    out: list[tuple[FollowUpView, Eligibility]] = []
    for v in (*panel.overdue, *panel.due_today)[:MAX_SCAN]:
        if v.kind not in DRAFTABLE_KINDS or v.prospect_id is None:
            continue
        f = db.get(FollowUp, v.id)
        if f is not None:
            out.append((v, check(db, f, member, today=today)))
    return out


def due_email_count(db: Session, member: TeamMember | None = None, *, today: date | None = None) -> int:
    """How many due reminders could be drafted right now (the button's count)."""
    today = today or _today()
    panel = followups.list_due(db, today=today)
    n = 0
    for v in (*panel.overdue, *panel.due_today)[:MAX_SCAN]:
        if v.kind in DRAFTABLE_KINDS and v.prospect_id is not None:
            f = db.get(FollowUp, v.id)
            if f is not None and check(db, f, member, today=today).ok:
                n += 1
    return n


__all__ = ["BatchResult", "DraftOutcome", "Eligibility", "check", "draft_due", "draft_follow_up", "draft_one", "due_email_count",
           "due_overview", "explain", "reply_seen"]
