"""Research synthesis: the final, structured brief, written by the research model (Sonnet) from everything gathered.

The cheap model reads the website (``loop.run_research``); this one call turns pages + web findings + our records into
the typed v4 record (profile, enquiry analysis, evidence-backed facts, gaps). Its answer is validated and every fact's
source is checked against what was really fed in. Returns ``None`` when the call fails for a non-fatal reason, so the
caller keeps the loop's own brief.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from sales0.agent.research.loop import FATAL_LLM_ERRORS, PROMPTS_DIR, ExtraSource, _env, inert  # noqa: F401
from sales0.agent.research.schemas import OWN_RECORDS, CompanyBrief, brief_tool_schema
from sales0.config import get_settings
from sales0.db.models import LlmCall
from sales0.llm import LLMBadResponse, LLMClient, LLMError
from sales0.llm.client import StructuredRequest
from sales0.logging import get_logger

log = get_logger("sales0.agent.research.synth")

PROMPT_VERSION = "research-synth-v2"  # v2: + key_people (top people with verified contact details)
TOOL_NAME = "record_research"
TOOL_DESCRIPTION = "Record the final structured research record for the prospect (company, enquiry, people, evidence)."
SECTION_WEIGHTS = {"profile": 25, "enquiry": 25, "people": 15, "fit": 15, "facts": 20}


@dataclass
class SynthResult:
    brief: CompanyBrief | None  # None: the model answered but not with a valid record
    cost_usd_micros: int
    model: str


def synth_tool_schema() -> dict[str, Any]:
    schema = brief_tool_schema()
    props = schema["properties"]
    props["profile"] = {
        "type": "object",
        "properties": {
            "industry": {"type": "string"}, "headquarters": {"type": "string"},
            "size_band": {"type": "string", "description": "e.g. '200-500 employees', or 'unknown'"},
            "business_model": {"type": "string"},
            "customer_segments": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["industry", "headquarters", "size_band", "business_model", "customer_segments"],
    }
    props["enquiry"] = {
        "type": "object",
        "properties": {
            "need": {"type": "string", "description": "what is really being asked, one or two sentences"},
            "audience": {"type": "string"}, "timing": {"type": "string"}, "budget_signal": {"type": "string"},
            "urgency": {"type": "string", "enum": ["high", "medium", "low", "unknown"]},
            "fit": {"type": "string", "enum": ["strong", "possible", "weak", "unknown"]},
            "open_questions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["need", "audience", "timing", "budget_signal", "urgency", "fit", "open_questions"],
    }
    props["facts"] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "section": {"type": "string", "enum": ["company", "enquiry", "people"]},
                "claim": {"type": "string"},
                "source": {"type": "string", "description": "exact URL from the input, or 'our records'"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            },
            "required": ["section", "claim", "source", "confidence"],
        },
    }
    props["gaps"] = {"type": "array", "items": {"type": "string"}}
    props["key_people"] = {
        "type": "array",
        "description": "up to 8 TOP people at the company: founder / CEO / MD and the heads of HR, L&D, talent, technology and the department the enquiry is about, plus the people in our records. Contact details ONLY when a source in the input shows them",
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"}, "title": {"type": "string"}, "department": {"type": "string"},
                "linkedin_url": {"type": "string", "description": "their LinkedIn profile URL exactly as it appears in the input, or ''"},
                "email": {"type": "string", "description": "a work email exactly as it appears in the input, or ''"},
                "phone": {"type": "string", "description": "a phone number exactly as it appears in the input, or ''"},
                "why": {"type": "string", "description": "one line: why they matter for this deal"},
                "source": {"type": "string", "description": "URL from the input, or 'our records'"},
            },
            "required": ["name", "title", "source"],
        },
    }
    schema["required"] = [*schema["required"], "profile", "enquiry", "facts", "gaps", "key_people"]
    return schema


def completeness(brief: CompanyBrief) -> int:
    """0-100: how much of the record is actually filled in (not 'unknown' / empty)."""

    def known(v: str | None) -> bool:
        return bool(v) and v.strip().lower() not in ("unknown", "")

    score = 0
    p, e = brief.profile, brief.enquiry
    if p is not None:
        score += round(SECTION_WEIGHTS["profile"] * sum(known(x) for x in (p.industry, p.headquarters, p.size_band, p.business_model)) / 4)
    if e is not None:
        score += round(SECTION_WEIGHTS["enquiry"] * sum(known(x) for x in (e.need, e.audience, e.timing, e.budget_signal)) / 4)
    if brief.people:
        score += SECTION_WEIGHTS["people"]
    if brief.matched_offering_name or known(brief.suggested_offering):
        score += SECTION_WEIGHTS["fit"]
    score += min(SECTION_WEIGHTS["facts"], 4 * len(brief.facts))
    return min(100, score)


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def _people(brief: CompanyBrief, allowed: set[str], haystack: str) -> list[Any]:
    """Keep a person only with a known source; keep each contact detail only if it appears in the input text (the
    model may not invent an email pattern or a phone number)."""
    low, digits = haystack.lower(), _digits(haystack)
    out, seen = [], set()
    for kp in brief.key_people:
        key = kp.name.lower()
        src = kp.source.strip()
        if not kp.name or key in seen or not (src.lower() == OWN_RECORDS or src in allowed or (src and src.lower() in low)):
            continue
        seen.add(key)
        email = kp.email if kp.email and "@" in kp.email and kp.email.lower() in low else ""
        phone = kp.phone if kp.phone and len(_digits(kp.phone)) >= 7 and _digits(kp.phone) in digits else ""
        li = kp.linkedin_url if "linkedin.com/in/" in kp.linkedin_url.lower() and kp.linkedin_url.lower().rstrip("/") in low else ""
        out.append(kp.model_copy(update={"email": email, "phone": phone, "linkedin_url": li,
                                         "source": OWN_RECORDS if src.lower() == OWN_RECORDS else src}))
    return out[:8]


def _clean(brief: CompanyBrief, allowed: set[str], offerings: list[str], haystack: str = "") -> CompanyBrief:
    """Keep only facts whose source was really in the input; normalise the matched offering; compute completeness."""
    facts = []
    for f in brief.facts:
        src = f.source.strip()
        if src.lower() == OWN_RECORDS:
            facts.append(f.model_copy(update={"source": OWN_RECORDS}))
        elif src in allowed:
            facts.append(f)
    by_lower = {n.lower(): n for n in offerings}
    matched = by_lower.get((brief.matched_offering_name or "").strip().lower())
    sources = [s for s in dict.fromkeys(brief.sources) if s in allowed] or sorted(allowed)
    out = brief.model_copy(update={
        "facts": facts[:16], "matched_offering_name": matched, "sources": sources, "gaps": brief.gaps[:6],
        "personalisation_hooks": brief.personalisation_hooks[:3], "enquiry_insights": brief.enquiry_insights[:4],
        "people": brief.people[:3], "key_people": _people(brief, allowed, haystack),
    })
    return out.model_copy(update={"completeness": completeness(out)})


def synthesize(
    db: Session,
    llm: LLMClient,
    *,
    company_name: str,
    website_url: str | None,
    context_text: str,
    pages: list[dict[str, str]],
    extras: list[ExtraSource],
    offerings: list[str],
    enquiry_id: int | None = None,
    now: datetime | None = None,
) -> SynthResult | None:
    settings = get_settings()
    stamp = now or datetime.now(UTC)
    user = _env().get_template("research_synth_user.md.j2").render(
        company_name=company_name, website_url=website_url or "(none)", context_text=context_text, offerings=offerings,
        pages=pages, extras=[{"kind": e.kind, "label": e.label, "url": e.url or "", "text": e.text} for e in extras],
    ).strip() + "\n"  # fmt: skip
    req = StructuredRequest(
        feature="research",
        system=(PROMPTS_DIR / "research_synth_system.md").read_text(encoding="utf-8").strip() + "\n",
        messages=[{"role": "user", "content": user}],
        tool_name=TOOL_NAME, tool_description=TOOL_DESCRIPTION, input_schema=synth_tool_schema(),
        max_tokens=settings.research_synthesis_max_tokens, prompt_version=PROMPT_VERSION,
        log_context={"enquiry_id": enquiry_id, "input_summary": f"research synthesis {company_name[:60]}"},
        model=settings.research_model,
    )  # fmt: skip
    try:
        resp = llm.structured(req)
    except FATAL_LLM_ERRORS as exc:
        db.add(LlmCall(feature="research", model=settings.research_model, prompt_version=PROMPT_VERSION, enquiry_id=enquiry_id,
                       status="error", error_type=str(getattr(exc, "error_type", type(exc).__name__))[:100], created_at=stamp))
        db.flush()
        raise
    except LLMError as exc:
        db.add(LlmCall(feature="research", model=settings.research_model, prompt_version=PROMPT_VERSION, enquiry_id=enquiry_id,
                       status="error", error_type=str(getattr(exc, "error_type", type(exc).__name__))[:100], created_at=stamp))
        db.flush()
        log.warning("research_synth_failed", error=type(exc).__name__)
        return None
    db.add(LlmCall(
        feature="research", model=resp.model[:100], prompt_version=PROMPT_VERSION, enquiry_id=enquiry_id,
        input_tokens=resp.input_tokens, output_tokens=resp.output_tokens, cost_usd_micros=resp.cost_usd_micros,
        latency_ms=resp.latency_ms, status="ok", request_id=resp.request_id[:100] if resp.request_id else None, created_at=stamp,
    ))
    db.flush()
    try:
        if resp.tool_input is None:
            raise LLMBadResponse("no record_research tool call in the answer")
        brief = CompanyBrief.model_validate(resp.tool_input)
    except (ValidationError, LLMBadResponse) as exc:
        log.warning("research_synth_invalid", error=str(exc).splitlines()[0][:160])
        return SynthResult(brief=None, cost_usd_micros=resp.cost_usd_micros, model=resp.model)
    allowed = {p["url"] for p in pages} | {e.url for e in extras if e.url} | {u for e in extras for u in e.citable}
    haystack = "\n".join([context_text, *(p.get("text", "") + " " + p.get("url", "") for p in pages), *(e.text + " " + (e.url or "") for e in extras)])
    return SynthResult(brief=_clean(brief, allowed, offerings, haystack), cost_usd_micros=resp.cost_usd_micros, model=resp.model)
