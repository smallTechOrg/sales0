"""Analytics: the pipeline funnel, time in each stage, why companies closed (by month and by offering), outreach
activity per week and per member, and an AI read of what worked and what did not.

Numbers are computed from the ledger (prospects / leads / clients), the companies' close reason and the timeline; the AI
read is built from the actual follow-up notes, email drafts and sent emails, replies and notes of the companies that
became leads / clients versus the ones that closed. Everything here is read-only except the stored AI read
(``text_settings`` key ``analytics_insights``)."""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc, local_today
from sales0.config import get_settings
from sales0.db.models import (
    Client, Company, CompanyOffering, Enquiry, FollowUpNote, Lead, LlmCall, Offering, OutreachMessage, OutreachReply, Prospect,
    TeamMember, TextSetting, TimelineEvent,
)
from sales0.logging import get_logger

log = get_logger("sales0.analytics")

PERIODS = {"30": 30, "90": 90, "365": 365, "all": None}
OFF_LABELS = {"not_interested": "Not interested / Lost", "dormant": "No response", "do_not_contact": "Do not contact",
              "lost_client": "Lost client"}
INSIGHTS_KEY = "analytics_insights"
PROMPT_VERSION = "analytics-insights-v1"
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


@dataclass
class FunnelStep:
    key: str
    label: str
    count: int
    pct_of_previous: float | None = None
    pct_of_first: float | None = None
    help: str = ""


@dataclass
class StageTime:
    label: str
    median_days: float | None
    mean_days: float | None
    n: int
    help: str = ""


@dataclass
class ActivityWeek:
    week_start: date
    emails: int = 0
    follow_ups: int = 0
    replies: int = 0
    meetings: int = 0


@dataclass
class MemberActivity:
    name: str
    emails: int = 0
    drafts: int = 0
    follow_ups: int = 0
    replies: int = 0
    meetings: int = 0


@dataclass
class Report:
    period: str
    offering_id: int | None
    funnel: list[FunnelStep]
    times: list[StageTime]
    close_by_month: list[tuple[str, dict[str, int]]]
    close_by_offering: list[tuple[str, dict[str, int]]]
    close_reasons: list[tuple[str, int]]
    weeks: list[ActivityWeek]
    members: list[MemberActivity]
    off_keys: list[str] = field(default_factory=lambda: list(OFF_LABELS))


def _since(period: str, now: datetime) -> datetime | None:
    days = PERIODS.get(period)
    return now - timedelta(days=days) if days else None


def _company_ids(db: Session, since: datetime | None, offering_id: int | None) -> set[int]:
    stmt = select(Company.id)
    if since is not None:
        stmt = stmt.where(Company.created_at >= since)
    if offering_id:
        stmt = stmt.where(Company.id.in_(select(CompanyOffering.company_id).where(CompanyOffering.offering_id == offering_id)))
    return set(db.scalars(stmt))


def _days(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    d = (ensure_utc(b) - ensure_utc(a)).total_seconds() / 86400
    return d if d >= 0 else None


def _stage_time(label: str, values: list[float], help_: str) -> StageTime:
    if not values:
        return StageTime(label, None, None, 0, help_)
    return StageTime(label, round(statistics.median(values), 1), round(statistics.fmean(values), 1), len(values), help_)


def funnel_and_times(db: Session, ids: set[int]) -> tuple[list[FunnelStep], list[StageTime]]:
    if not ids:
        return [FunnelStep(k, lbl, 0) for k, lbl in (("pipeline", "In the pipeline"), ("contacted", "Contacted"),
                                                         ("lead", "Became a lead"), ("client", "Became a client"))], []
    idl = list(ids)
    prospects = db.execute(select(Prospect.company_id, Prospect.created_at, Prospect.contacted_at).where(Prospect.company_id.in_(idl))).all()
    leads = db.execute(select(func.coalesce(Lead.company_id, Prospect.company_id), Lead.opened_at, Lead.stage, Lead.closed_at)
                       .join(Prospect, Prospect.id == Lead.prospect_id).where(func.coalesce(Lead.company_id, Prospect.company_id).in_(idl))).all()
    clients = db.execute(select(func.coalesce(Client.company_id, Prospect.company_id), Client.created_at)
                         .outerjoin(Prospect, Prospect.id == Client.prospect_id).where(func.coalesce(Client.company_id, Prospect.company_id).in_(idl))).all()
    in_pipe = set(db.scalars(select(Company.id).where(Company.id.in_(idl), Company.stage.is_not(None))))
    first_created: dict[int, datetime] = {}
    first_contact: dict[int, datetime] = {}
    for cid, created, contacted in prospects:
        if created and (cid not in first_created or ensure_utc(created) < first_created[cid]):
            first_created[cid] = ensure_utc(created)
        if contacted and (cid not in first_contact or ensure_utc(contacted) < first_contact[cid]):
            first_contact[cid] = ensure_utc(contacted)
    first_lead: dict[int, datetime] = {}
    won_at: dict[int, datetime] = {}
    for cid, opened, _stage, _closed in leads:
        if opened and (cid not in first_lead or ensure_utc(opened) < first_lead[cid]):
            first_lead[cid] = ensure_utc(opened)
    for cid, created in clients:
        if created and (cid not in won_at or ensure_utc(created) < won_at[cid]):
            won_at[cid] = ensure_utc(created)
    contacted = set(first_contact) | set(first_lead) | set(won_at)
    lead_set = set(first_lead) | set(won_at)
    steps = [
        ("pipeline", "In the pipeline", len(in_pipe | contacted), "Companies that entered the pipeline (an accepted enquiry or added by hand)."),
        ("contacted", "Contacted", len(contacted), "Someone reached out at least once (email, call, message, logged follow-up)."),
        ("lead", "Became a lead", len(lead_set), "A meeting or more: the company reached the Leads board."),
        ("client", "Became a client", len(set(won_at)), "Won: the company bought."),
    ]
    out: list[FunnelStep] = []
    first = steps[0][2] or 0
    prev = None
    for key, label, n, h in steps:
        out.append(FunnelStep(key, label, n, round(100 * n / prev, 1) if prev else None, round(100 * n / first, 1) if first else None, h))
        prev = n
    times = [
        _stage_time("New → Contacted", [d for c, t in first_contact.items() if (d := _days(first_created.get(c), t)) is not None],
                    "Days from entering the pipeline to the first outreach."),
        _stage_time("Contacted → Lead", [d for c, t in first_lead.items() if (d := _days(first_contact.get(c), t)) is not None],
                    "Days from the first outreach to becoming a lead (a meeting)."),
        _stage_time("Lead → Client", [d for c, t in won_at.items() if (d := _days(first_lead.get(c), t)) is not None],
                    "Days from becoming a lead to winning."),
    ]
    return out, times


def close_reasons(db: Session, ids: set[int]) -> tuple[list[tuple[str, dict[str, int]]], list[tuple[str, dict[str, int]]], list[tuple[str, int]]]:
    if not ids:
        return [], [], []
    rows = db.execute(select(Company.id, Company.off_ramp, Company.off_ramp_at, Company.off_ramp_reason)
                      .where(Company.id.in_(list(ids)), Company.off_ramp.is_not(None))).all()
    by_month: dict[str, dict[str, int]] = {}
    texts: dict[str, int] = {}
    for _cid, off, at, why in rows:
        month = ensure_utc(at).strftime("%Y-%m") if at else "unknown"
        by_month.setdefault(month, {}).setdefault(off, 0)
        by_month[month][off] += 1
        key = " ".join((why or "").split()).lower()[:80]
        if key:
            texts[key] = texts.get(key, 0) + 1
    names = dict(db.execute(select(Offering.id, Offering.name)).tuples().all())
    offer_of: dict[int, list[str]] = {}
    for cid, oid in db.execute(select(CompanyOffering.company_id, CompanyOffering.offering_id)
                               .where(CompanyOffering.company_id.in_([r[0] for r in rows] or [-1]))).tuples().all():
        offer_of.setdefault(cid, []).append(names.get(oid, f"#{oid}"))
    by_offering: dict[str, dict[str, int]] = {}
    for cid, off, _at, _why in rows:
        for name in offer_of.get(cid, ["No offering"]):
            by_offering.setdefault(name, {}).setdefault(off, 0)
            by_offering[name][off] += 1
    return (sorted(by_month.items(), reverse=True), sorted(by_offering.items(), key=lambda kv: -sum(kv[1].values())),
            sorted(texts.items(), key=lambda kv: -kv[1])[:10])


def _week(d: datetime) -> date:
    local = ensure_utc(d).astimezone(get_settings().tz).date()
    return local - timedelta(days=local.weekday())


def activity(db: Session, *, weeks: int = 12, now: datetime | None = None, ids: set[int] | None = None) -> tuple[list[ActivityWeek], list[MemberActivity]]:
    stamp = now or datetime.now(UTC)
    today = local_today(get_settings().tz, stamp)
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
    since = datetime.combine(start, datetime.min.time(), tzinfo=get_settings().tz).astimezone(UTC)
    rows = {start + timedelta(weeks=i): ActivityWeek(start + timedelta(weeks=i)) for i in range(weeks)}
    names = dict(db.execute(select(TeamMember.id, TeamMember.name)).tuples().all())
    members: dict[str, MemberActivity] = {}

    def member(mid: int | None) -> MemberActivity:
        name = names.get(mid, "Team / system") if mid else "Team / system"
        return members.setdefault(name, MemberActivity(name))

    def scoped(stmt, col):  # type: ignore[no-untyped-def]
        return stmt.where(col.in_(list(ids))) if ids is not None else stmt

    for at, mid in db.execute(scoped(select(OutreachMessage.sent_at, OutreachMessage.member_id).where(
            OutreachMessage.state == "sent", OutreachMessage.sent_at >= since), OutreachMessage.company_id)).all():
        if (w := rows.get(_week(at))) is not None:
            w.emails += 1
        member(mid).emails += 1
    for (mid,) in db.execute(scoped(select(OutreachMessage.member_id).where(
            OutreachMessage.state.in_(("draft", "approved")), OutreachMessage.created_at >= since), OutreachMessage.company_id)).all():
        member(mid).drafts += 1
    for created, how, mid in db.execute(scoped(select(FollowUpNote.created_at, FollowUpNote.how, FollowUpNote.actor_member_id).where(
            FollowUpNote.created_at >= since), FollowUpNote.company_id)).all():
        w = rows.get(_week(created))
        if how == "meeting":
            if w is not None:
                w.meetings += 1
            member(mid).meetings += 1
        else:
            if w is not None:
                w.follow_ups += 1
            member(mid).follow_ups += 1
    for at, kind, data, mid in db.execute(scoped(select(TimelineEvent.occurred_at, TimelineEvent.kind, TimelineEvent.data, TimelineEvent.actor_member_id).where(
            TimelineEvent.occurred_at >= since, TimelineEvent.kind.in_(("follow_up_done", "call", "meeting", "lead_created", "lead_stage")),
            TimelineEvent.subject_type.in_(("prospect", "lead"))), TimelineEvent.company_id)).all():
        d = data if isinstance(data, dict) else {}
        w = rows.get(_week(at))
        if kind in ("follow_up_done", "call"):
            if kind == "follow_up_done" and d.get("auto"):
                continue
            if w is not None:
                w.follow_ups += 1
            member(mid).follow_ups += 1
        elif kind == "meeting" or (kind == "lead_created" and d.get("stage") == "meeting_scheduled") or (kind == "lead_stage" and d.get("to") == "meeting_scheduled"):
            if w is not None:
                w.meetings += 1
            member(mid).meetings += 1
    rep = select(OutreachReply.received_at, OutreachReply.member_id).join(OutreachMessage, OutreachMessage.id == OutreachReply.message_id).where(
        OutreachReply.received_at >= since, OutreachReply.kind == "human")
    for at, mid in db.execute(scoped(rep, OutreachMessage.company_id)).all():
        if (w := rows.get(_week(at))) is not None:
            w.replies += 1
        member(mid).replies += 1
    return list(rows.values()), sorted(members.values(), key=lambda m: (-(m.emails + m.follow_ups + m.meetings), m.name))


def report(db: Session, *, period: str = "90", offering_id: int | None = None, now: datetime | None = None) -> Report:
    stamp = now or datetime.now(UTC)
    period = period if period in PERIODS else "90"
    ids = _company_ids(db, _since(period, stamp), offering_id)
    funnel, times = funnel_and_times(db, ids)
    by_month, by_offering, reasons = close_reasons(db, ids)
    weeks, members = activity(db, now=stamp, ids=_company_ids(db, None, offering_id) if offering_id else None)
    return Report(period, offering_id, funnel, times, by_month, by_offering, reasons, weeks, members)


# ---------------------------------------------------------------------------- the AI read: what worked, what did not
class Insight(BaseModel):
    point: str = Field(max_length=300)
    evidence: str = Field(default="", max_length=400)


class Insights(BaseModel):
    worked: list[Insight] = Field(default_factory=list, max_length=8)
    did_not_work: list[Insight] = Field(default_factory=list, max_length=8)
    patterns: list[str] = Field(default_factory=list, max_length=8)
    next_steps: list[str] = Field(default_factory=list, max_length=8)


def _schema() -> dict[str, Any]:
    item = {"type": "object", "properties": {"point": {"type": "string"}, "evidence": {"type": "string"}}, "required": ["point", "evidence"]}
    return {"type": "object", "properties": {
        "worked": {"type": "array", "items": item, "description": "what made companies move forward (lead / client), 3-6 items"},
        "did_not_work": {"type": "array", "items": item, "description": "what went wrong with companies that closed or stalled, 3-6 items"},
        "patterns": {"type": "array", "items": {"type": "string"}, "description": "patterns by offering, segment, channel, timing"},
        "next_steps": {"type": "array", "items": {"type": "string"}, "description": "concrete changes the team should make"},
    }, "required": ["worked", "did_not_work", "patterns", "next_steps"]}


def _clip(text: str | None, n: int) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _company_story(db: Session, co: Company, offers: dict[int, list[str]]) -> str:
    lines = [f"## {co.name} - outcome: {co.stage or 'none'}/{co.sub_status or ''}" + (f", closed: {co.off_ramp} ({co.off_ramp_reason or 'no reason'})" if co.off_ramp else ""),
             f"Offerings: {', '.join(offers.get(co.id, [])) or 'none'}"]
    for e in db.scalars(select(Enquiry).where(Enquiry.company_id == co.id, Enquiry.review_status == "accepted").limit(2)):
        lines.append(f"Enquiry: {_clip(e.requirement_summary or e.raw_text, 200)}")
    for n in db.scalars(select(FollowUpNote).where(FollowUpNote.company_id == co.id).order_by(FollowUpNote.happened_on).limit(6)):
        lines.append(f"Follow-up {n.happened_on} via {n.how}: {_clip(n.notes, 220)}")
    for m in db.scalars(select(OutreachMessage).where(OutreachMessage.company_id == co.id).order_by(OutreachMessage.created_at).limit(4)):
        lines.append(f"Email ({m.state}) '{_clip(m.subject, 100)}': {_clip(m.body, 260)}")
    for r in db.scalars(select(OutreachReply).join(OutreachMessage, OutreachMessage.id == OutreachReply.message_id)
                        .where(OutreachMessage.company_id == co.id).order_by(OutreachReply.received_at).limit(3)):
        lines.append(f"Reply ({r.kind}/{r.intent or '-'}): {_clip(r.snippet, 220)}")
    for t in db.scalars(select(TimelineEvent).where(TimelineEvent.company_id == co.id, TimelineEvent.kind == "note")
                        .order_by(TimelineEvent.occurred_at.desc()).limit(8)):
        d = t.data if isinstance(t.data, dict) else {}
        if d.get("system") or d.get("follow_up_note_id") or t.actor == "system":
            continue
        lines.append(f"Note: {_clip(t.body, 200)}")
    return "\n".join(lines)


def evidence(db: Session, *, per_side: int = 20) -> tuple[str, int]:
    """The text the AI reads: companies that moved forward vs companies that closed (most recent first)."""
    names = dict(db.execute(select(Offering.id, Offering.name)).tuples().all())
    offers: dict[int, list[str]] = {}
    for cid, oid in db.execute(select(CompanyOffering.company_id, CompanyOffering.offering_id)).tuples().all():
        offers.setdefault(cid, []).append(names.get(oid, f"#{oid}"))
    good = list(db.scalars(select(Company).where(Company.stage.in_(("lead", "client")), Company.off_ramp.is_(None))
                           .order_by(Company.stage_changed_at.desc().nulls_last(), Company.id.desc()).limit(per_side)))
    bad = list(db.scalars(select(Company).where(Company.off_ramp.is_not(None))
                          .order_by(Company.off_ramp_at.desc().nulls_last(), Company.id.desc()).limit(per_side)))
    stalled = list(db.scalars(select(Company).where(Company.stage == "prospect", Company.off_ramp.is_(None),
                                                    Company.sub_status.in_(("follow_up_2", "final_follow_up")))
                              .order_by(Company.updated_at.desc()).limit(per_side // 2)))
    parts = ["# Moved forward (lead or client)"] + [_company_story(db, c, offers) for c in good]
    parts += ["# Closed (lost, no response, not interested)"] + [_company_story(db, c, offers) for c in bad]
    parts += ["# Stalled (late follow-ups, no answer yet)"] + [_company_story(db, c, offers) for c in stalled]
    return "\n\n".join(parts)[:60_000], len(good) + len(bad) + len(stalled)


def stored_insights(db: Session) -> tuple[Insights | None, datetime | None, str | None]:
    row = db.get(TextSetting, INSIGHTS_KEY)
    if row is None or not row.value:
        return None, None, None
    try:
        data = json.loads(row.value)
        return Insights.model_validate(data.get("insights") or {}), ensure_utc(row.updated_at), row.updated_by
    except (ValueError, ValidationError):
        return None, None, None


def generate_insights(db: Session, *, actor: str, llm: Any = None) -> Insights:
    from sales0.errors import DomainError
    from sales0.llm import LLMError, get_llm_client
    from sales0.llm.client import StructuredRequest

    text, n = evidence(db)
    if n == 0:
        raise DomainError("nothing_to_analyse", "No company has moved forward or closed yet - nothing to learn from.", 409)
    settings = get_settings()
    req = StructuredRequest(
        feature="summary", model=settings.research_model,
        system=(PROMPTS_DIR / "analytics_insights_system.md").read_text(encoding="utf-8").strip() + "\n",
        messages=[{"role": "user", "content": "<pipeline_history>\n" + text.replace("</pipeline_history>", "") + "\n</pipeline_history>"}],
        tool_name="record_insights", tool_description="Record what worked and what did not.", input_schema=_schema(),
        max_tokens=2500, prompt_version=PROMPT_VERSION, log_context={"input_summary": f"analytics insights ({n} companies)"},
    )
    stamp = datetime.now(UTC)
    try:
        resp = (llm or get_llm_client()).structured(req)
    except LLMError as exc:
        db.add(LlmCall(feature="summary", model=settings.research_model[:100], prompt_version=PROMPT_VERSION, status="error",
                       error_type=type(exc).__name__[:100], created_at=stamp))
        db.flush()
        raise DomainError("llm_unavailable", "The AI could not be reached - try again in a minute.", 503) from exc
    db.add(LlmCall(feature="summary", model=resp.model[:100], prompt_version=PROMPT_VERSION, input_tokens=resp.input_tokens,
                   output_tokens=resp.output_tokens, cost_usd_micros=resp.cost_usd_micros, latency_ms=resp.latency_ms, status="ok",
                   created_at=stamp))
    try:
        result = Insights.model_validate(resp.tool_input or {})
    except ValidationError as exc:
        raise DomainError("llm_bad_response", "The AI answer could not be read - try again.", 502) from exc
    row = db.get(TextSetting, INSIGHTS_KEY)
    payload = json.dumps({"insights": result.model_dump(), "companies": n})
    if row is None:
        db.add(TextSetting(key=INSIGHTS_KEY, value=payload, updated_by=actor[:80], updated_at=stamp))
    else:
        row.value, row.updated_by, row.updated_at = payload, actor[:80], stamp
    db.flush()
    return result


__all__ = ["PERIODS", "Insights", "Report", "activity", "close_reasons", "evidence", "funnel_and_times", "generate_insights", "report",
           "stored_insights"]
