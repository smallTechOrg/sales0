"""Keyword ideas: ONE call on the strong model (``SALES0_KEYWORD_IDEAS_MODEL``, Opus) over what the searches actually
found - winning posts, noise posts and every keyword's performance - proposing new keywords and keywords to retire.

The model proposes; code decides: phrases are normalised, must be 2-100 characters, may not duplicate an existing
keyword (or each other), offerings must match by name, evidence numbers must point at a winning post, and a keyword
to retire must exist. Nothing is stored: the Keywords page shows the ideas as a review list the team ticks and adds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.agent.research.loop import PROMPTS_DIR, _env
from sales0.config import get_settings
from sales0.db.models import Enquiry, EnquiryKeywordHit, LlmCall, Offering
from sales0.errors import DomainError
from sales0.llm import LLMAuthError, LLMBadResponse, LLMClient, LLMError, LLMModelNotFound, get_llm_client
from sales0.llm.client import StructuredRequest
from sales0.logging import get_logger
from sales0.services._common import clip, now_or
from sales0.services.costs_report import ensure_within_spend_cap
from sales0.services.keyword_insights import KeywordStat, keyword_stats
from sales0.services.keywords import MAX_LEN, MIN_LEN, normalize_keyword

log = get_logger("sales0.keyword_ideas")

PROMPT_VERSION = "keyword-ideas-v1"
TOOL_NAME = "record_keyword_ideas"
FATAL = (LLMAuthError, LLMModelNotFound)
MAX_NEW = 20
MAX_RETIRE = 10
MAX_WINNERS = 30
MAX_NOISE = 15
MAX_KEYWORDS = 200
WINNER_CLIP = 400
NOISE_CLIP = 220
SNIPPET_CLIP = 160


class _Idea(BaseModel):
    phrase: str = Field(max_length=200)
    offering: str = Field(default="", max_length=200)
    kind: str = Field(default="new", max_length=20)
    based_on: str = Field(default="", max_length=200)
    reason: str = Field(default="", max_length=400)
    evidence: list[int] = Field(default_factory=list, max_length=10)

    @field_validator("evidence", mode="before")
    @classmethod
    def _ints(cls, v: Any) -> Any:
        return [int(x) for x in v if str(x).strip().lstrip("-").isdigit()] if isinstance(v, list) else []


class _Retire(BaseModel):
    keyword: str = Field(max_length=200)
    action: str = Field(default="pause", max_length=20)
    reason: str = Field(default="", max_length=400)
    better_phrase: str = Field(default="", max_length=200)


class _Output(BaseModel):
    suggestions: list[_Idea] = Field(default_factory=list, max_length=60)
    retire: list[_Retire] = Field(default_factory=list, max_length=40)
    avoid: list[str] = Field(default_factory=list, max_length=20)


def tool_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "suggestions": {"type": "array", "items": {"type": "object", "properties": {
                "phrase": {"type": "string", "description": "2-6 word LinkedIn search phrase"},
                "offering": {"type": "string", "description": "exact offering name, or ''"},
                "kind": {"type": "string", "enum": ["new", "variant", "replacement"]},
                "based_on": {"type": "string", "description": "the existing keyword it varies or replaces, or ''"},
                "reason": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "integer"}, "description": "winning post numbers"},
            }, "required": ["phrase", "offering", "kind", "reason", "evidence"]}},
            "retire": {"type": "array", "items": {"type": "object", "properties": {
                "keyword": {"type": "string"}, "action": {"type": "string", "enum": ["pause", "reword"]},
                "reason": {"type": "string"}, "better_phrase": {"type": "string"},
            }, "required": ["keyword", "action", "reason"]}},
            "avoid": {"type": "array", "items": {"type": "string"}, "description": "wording that attracts noise"},
        },
        "required": ["suggestions", "retire", "avoid"],
    }  # fmt: skip


@dataclass
class Idea:
    phrase: str
    offering_id: int | None
    offering_name: str | None
    kind: str
    based_on: str
    reason: str
    evidence: list[str]


@dataclass
class Retire:
    keyword_id: int
    keyword: str
    action: str
    reason: str
    better_phrase: str
    tier: str


@dataclass
class IdeasResult:
    ideas: list[Idea] = field(default_factory=list)
    retire: list[Retire] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    dropped: int = 0  # proposals code refused (duplicates, bad length, unknown keyword ...)
    cost_usd_micros: int = 0
    model: str | None = None
    winners: int = 0
    noise: int = 0


def _hits(db: Session, enquiry_ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    if not enquiry_ids:
        return out
    for eid, text in db.execute(select(EnquiryKeywordHit.enquiry_id, EnquiryKeywordHit.keyword_text)
                                .where(EnquiryKeywordHit.enquiry_id.in_(enquiry_ids))):
        lst = out.setdefault(eid, [])
        if text not in lst and not text.startswith("watch:"):
            lst.append(text)
    return out


def _posts(db: Session) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    win_rows = db.execute(
        select(Enquiry.id, Enquiry.raw_text, Enquiry.review_status)
        .where((Enquiry.review_status == "accepted") | ((Enquiry.ai_verdict == "genuine") & (Enquiry.review_status != "discarded")))
        .order_by((Enquiry.review_status == "accepted").desc(), Enquiry.date_added.desc(), Enquiry.id.desc())
        .limit(MAX_WINNERS)
    ).all()
    noise_rows = db.execute(
        select(Enquiry.id, Enquiry.raw_text)
        .where((Enquiry.review_status == "discarded") | ((Enquiry.ai_verdict == "discard") & (Enquiry.review_status != "accepted")))
        .order_by(Enquiry.date_added.desc(), Enquiry.id.desc()).limit(MAX_NOISE)
    ).all()
    hits = _hits(db, [r[0] for r in win_rows] + [r[0] for r in noise_rows])
    winners = [{"id": i, "text": clip(" ".join((t or "").split()), WINNER_CLIP) or "", "accepted": rs == "accepted",
                "keywords": hits.get(i, [])} for i, t, rs in win_rows]
    noise = [{"id": i, "text": clip(" ".join((t or "").split()), NOISE_CLIP) or "", "keywords": hits.get(i, [])}
             for i, t in noise_rows]
    return winners, noise


def _clean(out: _Output, stats: list[KeywordStat], offerings: dict[str, int], names: dict[int, str],
           winners: list[dict[str, Any]], result: IdeasResult) -> None:
    have = {s.text.lower() for s in stats}
    by_text = {s.text.lower(): s for s in stats}
    for idea in out.suggestions:
        phrase = normalize_keyword(idea.phrase).strip("\"'#")
        key = phrase.lower()
        if not (MIN_LEN <= len(phrase) <= MAX_LEN) or key in have or len(result.ideas) >= MAX_NEW:
            result.dropped += 1
            continue
        have.add(key)
        oid = offerings.get(idea.offering.strip().lower())
        evidence = [clip(winners[i - 1]["text"], SNIPPET_CLIP) or "" for i in dict.fromkeys(idea.evidence) if 1 <= i <= len(winners)]
        result.ideas.append(Idea(phrase=phrase, offering_id=oid, offering_name=names.get(oid) if oid else None,
                                 kind=idea.kind if idea.kind in ("new", "variant", "replacement") else "new",
                                 based_on=clip(idea.based_on, 100) or "", reason=clip(idea.reason, 300) or "", evidence=evidence[:3]))
    seen: set[int] = set()
    for r in out.retire:
        s = by_text.get(normalize_keyword(r.keyword).lower())
        if s is None or s.id in seen or len(result.retire) >= MAX_RETIRE:
            result.dropped += 1
            continue
        seen.add(s.id)
        better = normalize_keyword(r.better_phrase)
        result.retire.append(Retire(keyword_id=s.id, keyword=s.text, action="reword" if r.action == "reword" and better else "pause",
                                    reason=clip(r.reason, 300) or "", better_phrase=better[:MAX_LEN] if better.lower() not in have else "",
                                    tier=s.tier))
    result.avoid = [clip(" ".join(a.split()), 120) or "" for a in out.avoid if a.strip()][:8]


def generate(db: Session, llm: LLMClient | None = None, *, focus: str = "", now: datetime | None = None) -> IdeasResult:
    """One model call; flushed llm_calls row (the caller commits). Raises DomainError when the AI is unavailable and
    SpendCapReached before the call; ``LLMAuthError`` / ``LLMModelNotFound`` propagate after their error row."""
    settings = get_settings()
    stamp = now_or(now)
    ensure_within_spend_cap(db, stamp)
    all_stats = keyword_stats(db)  # duplicates are checked against EVERY keyword; the prompt gets the top MAX_KEYWORDS
    stats = sorted(all_stats, key=lambda s: (-s.accepted, -s.relevant, s.text.lower()))[:MAX_KEYWORDS]
    offering_rows = list(db.scalars(select(Offering).where(Offering.active.is_(True)).order_by(Offering.sort_order, Offering.id)))
    offerings = {o.name.strip().lower(): o.id for o in offering_rows}
    winners, noise = _posts(db)
    user = _env().get_template("keyword_ideas_user.md.j2").render(
        offerings=[{"name": o.name, "description": clip(" ".join((o.description or "").split()), 300)} for o in offering_rows],
        keywords=stats, winners=winners, noise=noise, focus=clip(" ".join((focus or "").split()), 300) or "",
        max_new=MAX_NEW, max_retire=MAX_RETIRE,
    ).strip() + "\n"  # fmt: skip
    req = StructuredRequest(
        feature="suggest", system=(PROMPTS_DIR / "keyword_ideas_system.md").read_text(encoding="utf-8").strip() + "\n",
        messages=[{"role": "user", "content": user}], tool_name=TOOL_NAME,
        tool_description="Record new keyword ideas, keywords to retire and wording to avoid.",
        input_schema=tool_schema(), max_tokens=settings.keyword_ideas_max_tokens, prompt_version=PROMPT_VERSION,
        log_context={"input_summary": f"keyword ideas ({len(stats)} keywords, {len(winners)} winners)"},
        model=settings.keyword_ideas_model,
    )  # fmt: skip

    def row(status: str, resp: Any = None, exc: Exception | None = None) -> LlmCall:
        return LlmCall(
            feature="suggest", model=(resp.model if resp else settings.keyword_ideas_model)[:100], prompt_version=PROMPT_VERSION,
            input_tokens=resp.input_tokens if resp else 0, output_tokens=resp.output_tokens if resp else 0,
            cost_usd_micros=resp.cost_usd_micros if resp else 0, latency_ms=resp.latency_ms if resp else None, status=status,
            error_type=str(getattr(exc, "error_type", type(exc).__name__))[:100] if exc else None,
            request_id=(resp.request_id or "")[:100] or None if resp else None, created_at=stamp,
        )  # fmt: skip

    try:
        resp = (llm or get_llm_client()).structured(req)
    except FATAL as exc:
        db.add(row("error", exc=exc))
        db.flush()
        raise
    except LLMError as exc:
        db.add(row("error", exc=exc))
        db.flush()
        raise DomainError("ideas_failed", "The AI service is unavailable right now. Try again in a minute.", 502) from exc
    db.add(row("ok", resp))
    db.flush()
    result = IdeasResult(cost_usd_micros=resp.cost_usd_micros, model=resp.model, winners=len(winners), noise=len(noise))
    try:
        if resp.tool_input is None:
            raise LLMBadResponse("no record_keyword_ideas call")
        out = _Output.model_validate(resp.tool_input)
    except (ValidationError, LLMBadResponse) as exc:
        log.warning("keyword_ideas_invalid", error=str(exc).splitlines()[0][:160])
        raise DomainError("ideas_failed", "The AI answer could not be used. Try again.", 502) from exc
    _clean(out, all_stats, offerings, {o.id: o.name for o in offering_rows}, winners, result)
    log.info("keyword_ideas", ideas=len(result.ideas), retire=len(result.retire), dropped=result.dropped,
             cost=result.cost_usd_micros, winners=len(winners), noise=len(noise))
    return result


__all__ = ["Idea", "IdeasResult", "Retire", "generate", "tool_schema"]
