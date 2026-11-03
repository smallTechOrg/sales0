"""The free-text follow-up log: a person types what happened ("Called Priya, she wants the deck, call back Tuesday");
ONE small AI call structures it into How / Date / Contact / Notes (+ an optional reminder), and the entry is added to
the prospect's log. A reminder asked for in the text becomes a ``reminders`` row (Due panel). Nothing is
pre-scheduled. When the AI is unavailable the note is still saved as typed (``structured_by = 'fallback'``).

First contact: a note on a prospect that is still New moves it to Contacted (channel = the note's How).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.clock import local_today
from sales0.config import get_settings
from sales0.db.models import Contact, ContactLink, FollowUpNote, LlmCall, Prospect
from sales0.errors import DomainError
from sales0.llm import LLMAuthError, LLMClient, LLMError, LLMModelNotFound, get_llm_client
from sales0.llm.client import StructuredRequest
from sales0.logging import get_logger
from sales0.services import reminders
from sales0.services._common import clip, not_found, now_or
from sales0.services.crm import timeline
from sales0.services.crm.actor import TEAM, Actor

log = get_logger("sales0.followup_log")

PROMPT_VERSION = "followup-note-v1"
TOOL_NAME = "record_follow_up"
HOWS = ("email", "call", "whatsapp", "linkedin", "meeting", "other")
HOW_LABELS = {"email": "Email", "call": "Call", "whatsapp": "WhatsApp", "linkedin": "LinkedIn", "meeting": "Meeting", "other": "Other"}
CHANNEL_OF = {"email": "email", "call": "call", "whatsapp": "whatsapp", "linkedin": "linkedin"}  # the stage engine's channels
MAX_TEXT = 2000
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


class _Structured(BaseModel):
    how: str = "other"
    date: str = ""
    contact: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=1000)
    remind_on: str = ""
    remind_note: str = Field(default="", max_length=300)

    @field_validator("how", mode="before")
    @classmethod
    def _how(cls, v: Any) -> Any:
        text = str(v or "").strip().lower()
        return text if text in HOWS else "other"

    @field_validator("date", "contact", "notes", "remind_on", "remind_note", mode="before")
    @classmethod
    def _text(cls, v: Any) -> Any:
        return "" if v is None else " ".join(str(v).split())


def tool_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "how": {"type": "string", "enum": list(HOWS)},
            "date": {"type": "string", "description": "YYYY-MM-DD the contact happened (today if not said)"},
            "contact": {"type": "string", "description": "the person, exact name from the known people when it is one of them"},
            "notes": {"type": "string", "description": "1-3 short sentences: what happened and what was agreed"},
            "remind_on": {"type": "string", "description": "YYYY-MM-DD of the next step, or ''"},
            "remind_note": {"type": "string", "description": "the next step in a few words, or ''"},
        },
        "required": ["how", "date", "contact", "notes", "remind_on", "remind_note"],
    }


@dataclass(frozen=True)
class NoteView:
    id: int
    happened_on: date
    how: str
    how_label: str
    contact_name: str | None
    contact_id: int | None
    notes: str
    raw_text: str
    reminder_on: date | None
    actor: str
    created_at: datetime
    structured_by: str


def _iso(text: str) -> date | None:
    try:
        return date.fromisoformat(text[:10]) if text else None
    except ValueError:
        return None


def _people(db: Session, p: Prospect) -> list[tuple[int, str]]:
    ids = set(db.scalars(select(ContactLink.contact_id).where(ContactLink.entity_type == "prospect", ContactLink.entity_id == p.id)))
    stmt = select(Contact.id, Contact.full_name).where(Contact.full_name.is_not(None), Contact.merged_into_id.is_(None))
    stmt = stmt.where(Contact.id.in_(ids) | (Contact.company_id == p.company_id)) if p.company_id else stmt.where(Contact.id.in_(ids or [-1]))
    return [(int(i), str(n)) for i, n in db.execute(stmt.order_by(Contact.id).limit(40))]


def _match(name: str, people: list[tuple[int, str]]) -> int | None:
    low = name.strip().lower()
    if not low:
        return None
    for cid, full in people:
        if full.lower() == low:
            return cid
    firsts = [cid for cid, full in people if full.lower().split()[0] == low.split()[0]]
    return firsts[0] if len(firsts) == 1 else None


def structure(db: Session, llm: LLMClient | None, text: str, *, today: date, people: list[str]) -> tuple[_Structured | None, int]:
    """(the structured reading or None when the AI was unavailable / unusable, its cost). Auth / model errors propagate."""
    known = "\n".join(f"- {n}" for n in people) or "- (none on file)"
    user = (f"Today is {today.isoformat()} ({today.strftime('%A')}).\n\nKnown people at this company:\n{known}\n\n"
            f"The note:\n<note>\n{text.replace('<', '&lt;').replace('>', '&gt;')}\n</note>\n")
    req = StructuredRequest(
        feature="summary", system=(PROMPTS_DIR / "followup_note_system.md").read_text(encoding="utf-8").strip() + "\n",
        messages=[{"role": "user", "content": user}], tool_name=TOOL_NAME,
        tool_description="Record the structured follow-up log entry.", input_schema=tool_schema(), max_tokens=500,
        prompt_version=PROMPT_VERSION, log_context={"input_summary": "follow-up note"},
    )  # fmt: skip
    stamp = datetime.now(UTC)
    try:
        resp = (llm or get_llm_client()).structured(req)
    except (LLMAuthError, LLMModelNotFound):
        raise
    except LLMError as exc:
        db.add(LlmCall(feature="summary", model=get_settings().llm_model, prompt_version=PROMPT_VERSION, status="error",
                       error_type=type(exc).__name__[:100], created_at=stamp))
        db.flush()
        return None, 0
    db.add(LlmCall(feature="summary", model=resp.model[:100], prompt_version=PROMPT_VERSION, input_tokens=resp.input_tokens,
                   output_tokens=resp.output_tokens, cost_usd_micros=resp.cost_usd_micros, latency_ms=resp.latency_ms, status="ok",
                   created_at=stamp))
    db.flush()
    try:
        return (_Structured.model_validate(resp.tool_input) if resp.tool_input else None), resp.cost_usd_micros
    except ValidationError:
        return None, resp.cost_usd_micros


def add_note(
    db: Session, llm: LLMClient | None, prospect_id: int, text: str, *, actor: Actor = TEAM, now: datetime | None = None,
) -> FollowUpNote:
    """Structure ``text`` and add it to the prospect's follow-up log (flushed; the caller commits)."""
    raw = (text or "").replace("\r\n", "\n").strip()
    if len(raw) < 3:
        raise DomainError("followup_invalid", "Write what happened (a few words is enough).", 400)
    if len(raw) > MAX_TEXT:
        raise DomainError("followup_invalid", f"Keep a follow-up note under {MAX_TEXT} characters.", 400)
    p = db.get(Prospect, prospect_id)
    if p is None:
        raise not_found()
    stamp = now_or(now)
    tz = get_settings().tz
    today = local_today(tz, stamp)
    people = _people(db, p)
    parsed, _cost = structure(db, llm, raw, today=today, people=[n for _i, n in people])
    if parsed is None:
        how, happened, contact, notes, remind_on, remind_note, by = "other", today, "", clip(raw, 1000) or raw[:1000], None, "", "fallback"
    else:
        happened = _iso(parsed.date) or today
        happened = min(happened, today)
        remind_on = _iso(parsed.remind_on)
        if remind_on is not None and remind_on < today:
            remind_on = None
        how, contact, notes, remind_note, by = parsed.how, parsed.contact, parsed.notes or clip(raw, 1000), parsed.remind_note, "ai"
    contact_id = _match(contact, people)
    if contact_id is not None:
        contact = next(n for i, n in people if i == contact_id)
    reminder = None
    if remind_on is not None:
        reminder = reminders.add(
            db, subject_type="prospect", subject_id=p.id, due_on=remind_on, note=remind_note or f"Follow up: {notes}"[:300],
            company_id=p.company_id, prospect_id=p.id, source="follow_up", created_by=actor.label, owner_member_id=actor.member_id,
            now=stamp,
        )  # fmt: skip
    row = FollowUpNote(
        prospect_id=p.id, company_id=p.company_id, raw_text=raw, how=how, notes=clip(notes, 1000) or "", happened_on=happened,
        contact_id=contact_id, contact_name=clip(contact, 200) or None, reminder_id=reminder.id if reminder else None,
        structured_by=by, actor=actor.label[:80], actor_member_id=actor.member_id, created_at=stamp,
    )  # fmt: skip
    db.add(row)
    db.flush()
    who = f" with {row.contact_name}" if row.contact_name else ""
    timeline.add_event(
        db, subject_type="prospect", subject_id=p.id, kind="note", actor=actor, occurred_at=stamp, prospect_id=p.id,
        company_id=p.company_id,
        body=f"Follow-up ({HOW_LABELS[how]}{who}, {happened.strftime('%d %b')}): {row.notes}"
        + (f" - reminder {remind_on.strftime('%d %b')}" if remind_on else ""),
        data={"follow_up_note_id": row.id, "how": how, "contact_id": contact_id, "reminder_id": row.reminder_id},
    )  # fmt: skip
    if p.status == "open" and p.stage == "new":  # the first logged contact moves the prospect to Contacted
        from sales0.services.crm import prospects

        when = datetime.combine(happened, time(12, 0), tzinfo=tz).astimezone(UTC) if happened != today else stamp
        prospects.mark_contacted(db, p.id, when=when, actor=actor, channel=CHANNEL_OF.get(how, "manual"), note=row.notes[:300], now=stamp)
    return row


def notes_for(db: Session, prospect_id: int, *, limit: int = 50) -> list[NoteView]:
    rows = db.scalars(select(FollowUpNote).where(FollowUpNote.prospect_id == prospect_id)
                      .order_by(FollowUpNote.happened_on.desc(), FollowUpNote.id.desc()).limit(limit)).all()
    from sales0.db.models import Reminder

    due = dict(db.execute(select(Reminder.id, Reminder.due_on).where(Reminder.id.in_([r.reminder_id for r in rows if r.reminder_id] or [-1]))).tuples().all())
    return [NoteView(id=r.id, happened_on=r.happened_on, how=r.how, how_label=HOW_LABELS.get(r.how, "Other"), contact_name=r.contact_name,
                     contact_id=r.contact_id, notes=r.notes, raw_text=r.raw_text, reminder_on=due.get(r.reminder_id),
                     actor=r.actor, created_at=r.created_at, structured_by=r.structured_by) for r in rows]


__all__ = ["HOWS", "HOW_LABELS", "NoteView", "add_note", "notes_for", "structure", "tool_schema"]

