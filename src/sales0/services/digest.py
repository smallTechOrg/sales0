"""Daily digest: a registry of sections built from live data, stored once per local date.

* Sections are registered with :func:`register_section` (Phase 2 adds follow-ups, stale leads,
  tracked-profile hits the same way, without touching this file). A builder that raises is
  isolated: its section shows an error item and the rest of the digest is still built.
* :func:`generate_digest` upserts one ``digests`` row per local date (rebuilding replaces it),
  optionally with a 5-line Haiku summary (feature ``suggest``, one call). Every number in the
  summary must appear in the sections data or the summary is dropped. An LLM failure or a
  reached spend cap never fails the digest.
* The Gmail step only ever creates a DRAFT in the acting member's own mailbox, addressed to
  themselves. Nothing here sends email.

Window: ``start <= t <= end`` where ``end`` is ``now`` and ``start = now - 24h``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc, local_today
from sales0.config import get_settings
from sales0.db.models import (
    Digest,
    Enquiry,
    LlmCall,
    OutreachMessage,
    ScrapeRun,
    TeamMember,
)
from sales0.errors import DomainError
from sales0.integrations.gmail.errors import GmailError
from sales0.llm import (
    LLMAuthError,
    LLMClient,
    LLMError,
    LLMModelNotFound,
    get_llm_client,
)
from sales0.llm.client import StructuredRequest
from sales0.logging import get_logger
from sales0.services import costs_report
from sales0.services._common import clip, not_found, now_or
from sales0.services.costs_report import SpendCapReached, ensure_within_spend_cap
from sales0.services.team import connections
from sales0.services.team.members import normalise_email

log = get_logger("sales0.digest")

PROMPT_VERSION = "digest-v1"  # bump on any prompt or schema edit
TOOL_NAME = "write_summary"
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
WINDOW = timedelta(hours=24)
TOP_ENQUIRIES = 5
LIST_CAP = 10
STUCK_AFTER = timedelta(minutes=5)
MAX_SUMMARY_LINES = 5

Item = str | dict[str, Any]  # a dict item carries "text" and optionally "href"


# ---------------------------------------------------------------------- registry
@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime


@dataclass
class SectionResult:
    items: list[Item] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    empty_text: str = "Nothing to report."


@dataclass(frozen=True)
class DigestSection:
    key: str
    title: str
    order: int
    build: Callable[[Session, Window], SectionResult]


_REGISTRY: dict[str, DigestSection] = {}


def register_section(section: DigestSection, *, replace: bool = True) -> DigestSection:
    """Add a section (same key replaces it unless ``replace=False``, which raises)."""
    if section.key in _REGISTRY and not replace:
        raise ValueError(f"digest section {section.key!r} is already registered")
    _REGISTRY[section.key] = section
    return section


def unregister_section(key: str) -> None:
    _REGISTRY.pop(key, None)


def sections() -> list[DigestSection]:
    """Registered sections in display order (``order`` then ``key``)."""
    return sorted(_REGISTRY.values(), key=lambda s: (s.order, s.key))


# ---------------------------------------------------------------------- views
@dataclass(frozen=True)
class SectionView:
    key: str
    title: str
    items: list[Item]
    stats: dict[str, Any]
    empty_text: str
    error: str | None = None


@dataclass(frozen=True)
class DigestView:
    id: int
    digest_date: date
    trigger: str
    window_start: datetime | None
    window_end: datetime | None
    sections: list[SectionView]
    summary_text: str | None
    summary_cost_usd_micros: int
    notes: list[str]
    gmail_draft_id: str | None
    gmail_draft_member_id: int | None
    created_at: datetime


def item_text(item: Item) -> str:
    return str(item.get("text", "")) if isinstance(item, dict) else str(item)


def item_href(item: Item) -> str | None:
    return item.get("href") if isinstance(item, dict) else None


def _usd(micros: int) -> str:
    return f"${micros / 1_000_000:.4f}"


def _between(col: Any, w: Window) -> Any:
    return and_(col.is_not(None), col >= w.start, col <= w.end)


# ---------------------------------------------------------------------- built-in sections
def _new_relevant_enquiries(db: Session, w: Window) -> SectionResult:
    base = [
        Enquiry.ai_verdict == "genuine",
        Enquiry.triage_status == "done",
        _between(Enquiry.date_added, w),
    ]
    rank = case(
        (Enquiry.priority == "high", 0), (Enquiry.priority == "medium", 1), else_=2
    )
    rows = db.scalars(
        select(Enquiry)
        .where(*base)
        .order_by(rank, Enquiry.date_added.desc(), Enquiry.id.desc())
        .limit(TOP_ENQUIRIES)
    ).all()
    total = int(db.scalar(select(func.count(Enquiry.id)).where(*base)) or 0)
    high = int(
        db.scalar(
            select(func.count(Enquiry.id)).where(*base, Enquiry.priority == "high")
        )
        or 0
    )
    items: list[Item] = []
    for e in rows:
        who = e.company or e.author_name or "Unknown company"
        what = clip(e.requirement_summary or e.ai_reason or e.raw_text, 140) or ""
        items.append(
            {"text": f"{who} - {what} [{e.priority or 'unrated'}]", "href": "/inbox"}
        )
    return SectionResult(
        items,
        {"new_relevant": total, "high_priority": high, "shown": len(rows)},
        "No new relevant enquiries in the last 24 hours.",
    )


def _awaiting_review(db: Session, _w: Window) -> SectionResult:
    base = [Enquiry.review_status == "pending_review"]
    pending = int(db.scalar(select(func.count(Enquiry.id)).where(*base)) or 0)
    genuine = int(
        db.scalar(
            select(func.count(Enquiry.id)).where(*base, Enquiry.ai_verdict == "genuine")
        )
        or 0
    )
    high = int(
        db.scalar(
            select(func.count(Enquiry.id)).where(
                *base, Enquiry.ai_verdict == "genuine", Enquiry.priority == "high"
            )
        )
        or 0
    )
    items: list[Item] = []
    if pending:
        items.append(
            {
                "text": f"{pending} waiting for review ({genuine} genuine, {high} of them high priority)",
                "href": "/inbox?tab=review",
            }
        )
    return SectionResult(
        items,
        {"pending_review": pending, "genuine": genuine, "high": high},
        "Nothing is waiting for review.",
    )


def _outreach(db: Session, w: Window) -> SectionResult:
    def count(*where: Any) -> int:
        return int(db.scalar(select(func.count(OutreachMessage.id)).where(*where)) or 0)

    drafted = count(_between(OutreachMessage.created_at, w))
    approved = count(_between(OutreachMessage.approved_at, w))
    sent = count(_between(OutreachMessage.sent_at, w))
    failed_where = [
        OutreachMessage.state == "failed",
        _between(OutreachMessage.updated_at, w),
    ]
    failed = count(*failed_where)
    stuck_where = [
        OutreachMessage.state == "sending",
        OutreachMessage.updated_at < w.end - STUCK_AFTER,
    ]
    stuck = count(*stuck_where)
    names = {m.id: m.name for m in db.scalars(select(TeamMember))}
    items: list[Item] = []
    for msg in db.scalars(
        select(OutreachMessage)
        .where(*failed_where)
        .order_by(OutreachMessage.updated_at.desc())
        .limit(LIST_CAP)
    ):
        err = clip(" ".join((msg.error or "unknown error").split()), 120)
        href = f"/outreach/messages/{msg.id}"
        items.append(
            {
                "text": f'FAILED send by {names.get(msg.member_id, "?")}: "{clip(msg.subject, 60)}" - {err}',
                "href": href,
            }
        )
    for msg in db.scalars(
        select(OutreachMessage)
        .where(*stuck_where)
        .order_by(OutreachMessage.updated_at)
        .limit(LIST_CAP)
    ):
        items.append({
            "text": f"STUCK in Sending (check Gmail Sent before retrying): \"{clip(msg.subject, 60)}\" by {names.get(msg.member_id, '?')}",
            "href": f"/outreach/messages/{msg.id}",
        })  # fmt: skip
    stats = {
        "drafted": drafted,
        "approved": approved,
        "sent": sent,
        "failed": failed,
        "stuck_sending": stuck,
    }
    if drafted or approved or sent:
        items.insert(
            0,
            f"{drafted} drafted, {approved} approved, {sent} sent in the last 24 hours",
        )
    return SectionResult(items, stats, "No outreach activity in the last 24 hours.")


def _scrape_health(db: Session, w: Window) -> SectionResult:
    from sales0.services import runs as runs_service
    from sales0.services import session as session_service

    rows = db.scalars(
        select(ScrapeRun)
        .where(_between(ScrapeRun.started_at, w))
        .order_by(ScrapeRun.started_at.desc(), ScrapeRun.id.desc())
    ).all()
    items: list[Item] = []
    for r in rows[:LIST_CAP]:
        line = (
            f"Run #{r.id} {r.status}: {r.posts_new} new posts, {r.errors_count} errors"
        )
        if r.abort_reason:
            line += f", stopped: {r.abort_reason}"
        items.append({"text": line, "href": f"/runs/{r.id}"})
    cooldown = runs_service.safety_cooldown_until(db, w.end)
    if cooldown is not None:
        items.append(
            f"LinkedIn safety cool-down in effect until {cooldown.astimezone(get_settings().tz).strftime('%H:%M')}"
        )
    status = session_service.session_status(db)
    needs_attention = status.stored_status == "needs_attention"
    if needs_attention:
        items.append(
            {
                "text": f"LinkedIn session needs attention: {clip(status.last_error, 140) or 'reconnect it on Setup'}",
                "href": "/setup",
            }
        )
    stats = {
        "runs": len(rows),
        "posts_new": sum(r.posts_new for r in rows),
        "errors": sum(r.errors_count for r in rows),
        "aborted": sum(1 for r in rows if r.status == "aborted"),
        "cooldown_active": cooldown is not None,
        "session_needs_attention": needs_attention,
    }
    return SectionResult(items, stats, "No discovery runs in the last 24 hours.")


def _cost_today(db: Session, w: Window) -> SectionResult:
    rep = costs_report.build_report(db, w.end, days=1, months=1)
    cap = costs_report.spend_cap_status(db, w.end)
    items: list[Item] = [
        f"AI spend today {_usd(rep.today_micros)} ({rep.today_calls} calls); this month {_usd(rep.month_micros)}"
    ]
    if cap.state in ("warn", "exceeded"):
        items.append(cap.message or f"Monthly AI spend cap at {cap.pct:.0f}%")
    stats = {
        "today_usd": _usd(rep.today_micros), "today_calls": rep.today_calls, "month_usd": _usd(rep.month_micros),
        "cap_state": cap.state,
    }  # fmt: skip
    return SectionResult(items, stats)


def _register_builtins() -> None:
    for order, key, title, fn in (
        (
            10,
            "new_relevant_enquiries",
            "New relevant enquiries",
            _new_relevant_enquiries,
        ),
        (20, "awaiting_review", "Awaiting review", _awaiting_review),
        (30, "outreach", "Outreach", _outreach),
        (40, "scrape_health", "Discovery health", _scrape_health),
        (50, "cost_today", "AI cost", _cost_today),
    ):
        register_section(DigestSection(key, title, order, fn))


_register_builtins()


# ---------------------------------------------------------------------- summary
@lru_cache(maxsize=1)
def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(PROMPTS_DIR),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    env.filters["inert"] = lambda v: str(v).replace("<", "&lt;").replace(">", "&gt;")
    return env


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _numbers(text: str) -> set[str]:
    return {m.group(0).rstrip(".,") for m in _NUMBER.finditer(text)}


def unsupported_numbers(summary: str, data_text: str) -> list[str]:
    """Numbers quoted in ``summary`` that appear nowhere in ``data_text``."""
    allowed = _numbers(data_text)
    return sorted(n for n in _numbers(summary) if n not in allowed)


def _summary_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "lines": {
                "type": "array", "maxItems": MAX_SUMMARY_LINES, "minItems": 1,
                "items": {"type": "string"},
                "description": "1-5 short plain sentences, most important first",
            }
        },
        "required": ["lines"],
    }  # fmt: skip


def _summarise(
    db: Session,
    llm: LLMClient | None,
    sections_data: list[dict[str, Any]],
    digest_date: date,
    w: Window,
    now: datetime,
) -> tuple[str | None, int, str | None]:
    """(summary_text, cost_micros, note). Never raises for cap/LLM problems."""
    try:
        ensure_within_spend_cap(db, now)
    except SpendCapReached:
        return None, 0, "AI summary skipped: the monthly spend cap is reached."
    client = llm
    if client is None:
        if not get_settings().api_key_present:
            return None, 0, "AI summary skipped: no Anthropic key is configured."
        client = get_llm_client()
    data_text = json.dumps(sections_data, ensure_ascii=False, sort_keys=True)
    user = _env().get_template("digest_user.md.j2").render(
        digest_date=digest_date.isoformat(), window_start=w.start.isoformat(timespec="minutes"),
        window_end=w.end.isoformat(timespec="minutes"), sections_json=data_text,
    )  # fmt: skip
    req = StructuredRequest(
        feature="suggest",
        system=(PROMPTS_DIR / "digest_system.md").read_text(encoding="utf-8").strip()
        + "\n",
        messages=[{"role": "user", "content": user.strip() + "\n"}],
        tool_name=TOOL_NAME,
        tool_description="Submit the digest summary as 1 to 5 short lines.",
        input_schema=_summary_schema(),
        max_tokens=500,
        prompt_version=PROMPT_VERSION,
        log_context={"input_summary": f"digest {digest_date.isoformat()}"},
    )
    try:
        resp = client.structured(req)
    except (LLMError, LLMAuthError, LLMModelNotFound) as exc:
        db.add(LlmCall(
            feature="suggest", model=get_settings().llm_model, prompt_version=PROMPT_VERSION, status="error",
            error_type=str(getattr(exc, "error_type", type(exc).__name__))[:100], created_at=now,
        ))  # fmt: skip
        log.warning("digest_summary_failed", error=type(exc).__name__)
        return (
            None,
            0,
            "AI summary unavailable right now; the digest below is complete.",
        )
    db.add(LlmCall(
        feature="suggest", model=resp.model[:100], prompt_version=PROMPT_VERSION, input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens, cost_usd_micros=resp.cost_usd_micros, latency_ms=resp.latency_ms,
        status="ok", request_id=resp.request_id[:100] if resp.request_id else None, created_at=now,
    ))  # fmt: skip
    raw = (resp.tool_input or {}).get("lines")
    lines = [" ".join(str(x).split()) for x in raw] if isinstance(raw, list) else []
    lines = [x for x in lines if x][:MAX_SUMMARY_LINES]
    if not lines:
        return None, resp.cost_usd_micros, "AI summary was empty and was left out."
    bad = unsupported_numbers("\n".join(lines), data_text)
    if bad:
        return (
            None,
            resp.cost_usd_micros,
            f"AI summary left out: it quoted figures not in the data ({', '.join(bad)}).",
        )
    return "\n".join(lines), resp.cost_usd_micros, None


# ---------------------------------------------------------------------- build / store
def _build_sections(db: Session, w: Window) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sec in sections():
        try:
            with db.begin_nested():
                res = sec.build(db, w)
            out.append({
                "key": sec.key, "title": sec.title, "items": list(res.items), "stats": dict(res.stats),
                "empty_text": res.empty_text, "error": None,
            })  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - one broken section must not break the digest
            log.warning(
                "digest_section_failed", section=sec.key, error=type(exc).__name__
            )
            out.append({
                "key": sec.key, "title": sec.title, "items": [f"This section could not be built ({type(exc).__name__})."],
                "stats": {}, "empty_text": "", "error": f"{type(exc).__name__}: {clip(str(exc), 200)}",
            })  # fmt: skip
    return out


def view_of(row: Digest) -> DigestView:
    try:
        data = json.loads(row.sections_json)
    except ValueError:
        data = {}
    if isinstance(data, list):  # tolerate a bare sections list
        data = {"sections": data}
    win = data.get("window") or {}

    def parse(v: Any) -> datetime | None:
        try:
            return ensure_utc(datetime.fromisoformat(v)) if v else None
        except ValueError:
            return None

    secs = [
        SectionView(
            key=str(s.get("key", "")), title=str(s.get("title", "")), items=list(s.get("items") or []),
            stats=dict(s.get("stats") or {}), empty_text=str(s.get("empty_text") or "Nothing to report."), error=s.get("error"),
        )
        for s in data.get("sections") or []
    ]  # fmt: skip
    return DigestView(
        id=row.id, digest_date=row.digest_date, trigger=row.trigger, window_start=parse(win.get("start")),
        window_end=parse(win.get("end")), sections=secs, summary_text=row.summary_text,
        summary_cost_usd_micros=row.summary_cost_usd_micros, notes=[str(n) for n in data.get("notes") or []],
        gmail_draft_id=row.gmail_draft_id, gmail_draft_member_id=row.gmail_draft_member_id, created_at=row.created_at,
    )  # fmt: skip


def generate_digest(
    db: Session,
    *,
    trigger: str = "manual",
    now: datetime | None = None,
    llm: LLMClient | None = None,
    with_summary: bool | None = None,
) -> DigestView:
    """Build (or rebuild) the digest for the local date of ``now`` and commit it. Sends nothing."""
    if trigger not in ("manual", "scheduled"):
        raise DomainError(
            "bad_request", "trigger must be 'manual' or 'scheduled'.", 400
        )
    settings = get_settings()
    stamp = ensure_utc(now_or(now))
    w = Window(stamp - WINDOW, stamp)
    digest_date = local_today(settings.tz, stamp)
    secs = _build_sections(db, w)
    notes: list[str] = []
    summary: str | None = None
    cost = 0
    want = settings.digest_ai_summary if with_summary is None else with_summary
    if want:
        summary, cost, note = _summarise(db, llm, secs, digest_date, w, stamp)
        if note:
            notes.append(note)
    payload = json.dumps(
        {"window": {"start": w.start.isoformat(), "end": w.end.isoformat()}, "sections": secs, "notes": notes},
        ensure_ascii=False, default=str,
    )  # fmt: skip
    row = db.scalar(select(Digest).where(Digest.digest_date == digest_date))
    if row is None:
        row = Digest(digest_date=digest_date)
        db.add(row)
    row.trigger = trigger
    row.sections_json = payload
    row.summary_text = summary
    row.summary_cost_usd_micros = cost
    row.gmail_draft_id = None
    row.gmail_draft_member_id = None
    row.created_at = stamp
    db.commit()
    log.info(
        "digest_built",
        digest_id=row.id,
        date=digest_date.isoformat(),
        trigger=trigger,
        summary=summary is not None,
    )
    return view_of(row)


# ---------------------------------------------------------------------- reads
def latest_digest(db: Session) -> DigestView | None:
    row = db.scalar(
        select(Digest).order_by(Digest.digest_date.desc(), Digest.id.desc()).limit(1)
    )
    return view_of(row) if row else None


def get_digest(db: Session, digest_id: int) -> DigestView:
    row = db.get(Digest, digest_id)
    if row is None:
        raise not_found()
    return view_of(row)


def list_digests(db: Session, limit: int = 30) -> list[DigestView]:
    rows = db.scalars(
        select(Digest)
        .order_by(Digest.digest_date.desc(), Digest.id.desc())
        .limit(limit)
    ).all()
    return [view_of(r) for r in rows]


def digest_subject(digest: DigestView) -> str:
    return f"sales0 daily digest - {digest.digest_date.isoformat()}"


def digest_text(digest: DigestView) -> str:
    """Plain-text rendering (copy-to-clipboard and the Gmail draft body)."""
    lines = [digest_subject(digest), ""]
    if digest.summary_text:
        lines += [
            "At a glance",
            *[f"  {x}" for x in digest.summary_text.splitlines()],
            "",
        ]
    for note in digest.notes:
        lines += [f"Note: {note}", ""]
    for sec in digest.sections:
        lines.append(sec.title.upper())
        if sec.items:
            lines += [f"  - {item_text(i)}" for i in sec.items]
        else:
            lines.append(f"  {sec.empty_text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------- Gmail (draft only)
def create_gmail_draft_to_self(db: Session, digest_id: int, member: TeamMember) -> str:
    """Create a Gmail DRAFT of the digest addressed to ``member`` in ``member``'s own mailbox.

    Never sends: only ``create_draft`` is called. Returns the draft id (stored on the digest)."""
    row = db.get(Digest, digest_id)
    if row is None:
        raise not_found()
    if not member.active:
        raise DomainError("member_inactive", "This team member is deactivated.", 403)
    view = connections.connection_status(db, member.id)
    if not view.usable:
        raise DomainError(
            "gmail_not_connected",
            f"Gmail is not ready for {member.name} ({view.label}). Open Team and press Connect my Gmail.",
            409,
        )
    digest = view_of(row)
    try:
        client = connections.client_for_member(member.id)
        profile = normalise_email(client.profile_email())
        if profile != normalise_email(member.email):
            raise DomainError(
                "sender_mismatch",
                f"This Gmail connection is {profile}, not {member.email} ({member.name}). "
                "No draft was created - reconnect Gmail with the right account on the Team page.",
                409,
            )
        draft_id = client.create_draft(
            to=[member.email],
            cc=[],
            subject=digest_subject(digest),
            text_body=digest_text(digest),
        )
    except GmailError as exc:
        connections.record_failure(member.id, exc)
        raise DomainError(
            "gmail_unavailable",
            f"Gmail could not be reached for {member.name}: {exc}",
            409,
        ) from exc
    row.gmail_draft_id = draft_id
    row.gmail_draft_member_id = member.id
    db.commit()
    log.info("digest_draft_created", digest_id=digest_id, member_id=member.id)
    return draft_id


__all__ = [
    "DigestSection", "DigestView", "SectionResult", "SectionView", "Window",
    "create_gmail_draft_to_self", "digest_subject", "digest_text", "generate_digest", "get_digest",
    "item_href", "item_text", "latest_digest", "list_digests", "register_section", "sections",
    "unregister_section", "unsupported_numbers",
]  # fmt: skip
