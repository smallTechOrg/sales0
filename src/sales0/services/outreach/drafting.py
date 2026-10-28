"""AI drafting: one Haiku call (forced tool use) turns template + enquiry + brief into a draft message.

The model is told the rules (no invented facts/prices, <= 180 words, no placeholders, brief
hooks, signature) and the code verifies them afterwards: on a hard problem it asks once more
with the problems listed; whatever still fails is stored in ``error`` ("Draft needs attention")
so the human sees it, and Approve stays blocked for unfilled placeholders.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.models import Enquiry, LlmCall, Offering, OutreachMessage, TeamMember
from sales0.errors import DomainError
from sales0.llm import LLMAuthError, LLMBadResponse, LLMClient, LLMError, LLMModelNotFound, get_llm_client
from sales0.llm.client import StructuredRequest
from sales0.logging import get_logger
from sales0.services._common import clip, now_or
from sales0.services.costs_report import ensure_within_spend_cap
from sales0.services import suppressions
from sales0.services.outreach import decks, templates
from sales0.services.outreach._shared import add_event, join_addrs, split_addrs
from sales0.services.team.connections import connection_status

log = get_logger("sales0.outreach.drafting")

PROMPT_VERSION = "draft-v1"  # bump on any prompt or schema edit
TOOL_NAME = "write_email"
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
FATAL_LLM_ERRORS = (LLMAuthError, LLMModelNotFound)
TARGET_WORDS = 180
MAX_WORDS = 200

_PRICE = re.compile(
    r"(?:₹|\$|€|£|\bRs\.?|\bINR\b|\bUSD\b)\s?\d[\d,.]*|\b\d[\d,.]*\s?(?:rupees|lakhs?|crores?)\b|\b\d+\s?%\s?(?:off|discount)|\bdiscount\b",
    re.I,
)
_WORD = re.compile(r"[A-Za-z]{4,}")


def _inert(value: object) -> str:
    return str(value).replace("<", "&lt;").replace(">", "&gt;")


@lru_cache(maxsize=1)
def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(PROMPTS_DIR), autoescape=False, undefined=StrictUndefined, keep_trailing_newline=True
    )
    env.filters["inert"] = _inert
    return env


def _tool_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "email subject, under 80 characters"},
            "body": {"type": "string", "description": "plain-text body, at most 180 words, ending with the sender's name"},
        },
        "required": ["subject", "body"],
    }


def word_count(text: str) -> int:
    return len(text.split())


def check_draft(
    subject: str, body: str, *, company: str, hooks: list[str], source_text: str, sender_name: str
) -> tuple[list[str], list[str]]:
    """(hard problems, soft problems) of a generated draft. Pure; used for regeneration and flagging."""
    hard: list[str] = []
    soft: list[str] = []
    left = templates.find_placeholders(subject) + templates.find_placeholders(body)
    if left:
        hard.append(f"Unfilled placeholder(s) remain: {', '.join(dict.fromkeys(left))}. Remove them or write real text.")
    words = word_count(body)
    if words > MAX_WORDS:
        hard.append(f"The body is {words} words; it must be at most {TARGET_WORDS}.")
    src = source_text.lower()
    for m in _PRICE.finditer(subject + "\n" + body):
        if m.group(0).lower() not in src:
            hard.append(f"It mentions a price/discount ('{m.group(0).strip()}') that is not in the inputs; remove it.")
            break
    if not subject.strip() or not body.strip():
        hard.append("Subject and body must both be non-empty.")
    if company and company.lower() not in (subject + " " + body).lower():
        soft.append(f"It never names the company '{company}'.")
    if hooks and not _uses_a_hook(body, hooks):
        soft.append("It uses none of the brief's personalisation hooks; weave in one or two.")
    if sender_name and sender_name.lower() not in body.lower():
        soft.append("It is not signed with the sender's name.")
    return hard, soft


def _uses_a_hook(body: str, hooks: list[str]) -> bool:
    body_words = {w.lower() for w in _WORD.findall(body)}
    for hook in hooks:
        hook_words = {w.lower() for w in _WORD.findall(hook)}
        if not hook_words:
            continue
        need = 2 if len(hook_words) >= 2 else 1
        if len(hook_words & body_words) >= need:
            return True
    return False


def _brief_parts(brief: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    """(plain fact lines, hooks) from a stored brief dict."""
    if not brief:
        return [], []
    lines: list[str] = []
    for key, label in (("what_they_do", "What they do"), ("size_hints", "Size"), ("suggested_offering", "Fit")):
        v = str(brief.get(key) or "").strip()
        if v and v.lower() != "unknown":
            lines.append(f"{label}: {v}")
    needs = [str(x) for x in brief.get("ai_training_needs") or [] if str(x).strip()]
    if needs:
        lines.append("Possible AI-training needs (hypotheses): " + "; ".join(needs))
    hooks = [str(h).strip() for h in brief.get("personalisation_hooks") or [] if str(h).strip()]
    return lines, hooks[:3]


def _llm_row(resp: Any, enquiry_id: int | None, now: datetime) -> LlmCall:
    return LlmCall(
        feature="drafting", model=resp.model[:100], prompt_version=PROMPT_VERSION, enquiry_id=enquiry_id,
        input_tokens=resp.input_tokens, output_tokens=resp.output_tokens, cost_usd_micros=resp.cost_usd_micros,
        latency_ms=resp.latency_ms, status="ok", request_id=resp.request_id[:100] if resp.request_id else None,
        created_at=now,
    )  # fmt: skip


def _error_row(exc: Exception, enquiry_id: int | None, now: datetime) -> LlmCall:
    return LlmCall(
        feature="drafting", model=get_settings().llm_model, prompt_version=PROMPT_VERSION, enquiry_id=enquiry_id,
        status="error", error_type=str(getattr(exc, "error_type", type(exc).__name__))[:100], created_at=now,
    )  # fmt: skip


def generate_draft(
    db: Session,
    llm: LLMClient | None,
    member: TeamMember,
    *,
    to: list[str] | str,
    cc: list[str] | str | None = None,
    offering_id: int | None = None,
    template_id: int | None = None,
    first_name: str | None = None,
    company: str,
    requirement: str,
    brief: dict[str, Any] | None = None,
    deck_id: int | None = None,
    enquiry_id: int | None = None,
    prospect_id: int | None = None,
    company_id: int | None = None,
    now: datetime | None = None,
) -> OutreachMessage:
    """Create an ``OutreachMessage`` in state ``draft`` owned by ``member`` (flushed; caller commits).

    ``template_id=None`` picks the active template for the offering (else a generic one, else none).
    ``deck_id=None`` picks the deck mapped to the offering. ``requirement`` is the enquiry text.
    """
    suppressions.assert_not_suppressed(db, to, cc)
    stamp = now_or(now)
    ensure_within_spend_cap(db, stamp)  # Phase 4: no LLM call over the monthly cap
    company = " ".join(company.split())
    first_name = (first_name or "").strip() or None
    offering = db.get(Offering, offering_id) if offering_id is not None else None
    template = templates.get_template(db, template_id) if template_id is not None else templates.template_for_offering(db, offering_id)
    deck = decks.get_deck(db, deck_id) if deck_id is not None else decks.deck_for_offering(db, offering_id)
    if deck is not None and not deck.active:
        deck = None
    ref_enquiry = enquiry_id if enquiry_id is not None and db.get(Enquiry, enquiry_id) is not None else None

    values = templates.template_values(
        first_name=first_name, company=company, requirement=clip(requirement, 300) or "",
        sender_name=member.name, offering=offering.name if offering else None,
    )  # fmt: skip
    t_subject = templates.render(template.subject, values).text if template else ""
    t_body = templates.render(template.body, values).text if template else ""
    facts, hooks = _brief_parts(brief)
    source_text = "\n".join([requirement, t_subject, t_body, *facts, *hooks, offering.name if offering else ""])

    client = llm or get_llm_client()
    feedback: list[str] = []
    best: tuple[str, str, list[str], list[str]] | None = None
    for attempt in (1, 2):
        user = _env().get_template("draft_user.md.j2").render(
            sender_name=member.name, first_name=first_name, company=company, requirement=requirement[:4000],
            offering=offering.name if offering else None, hooks=hooks, brief_facts=facts,
            template_subject=t_subject, template_body=t_body, feedback=feedback,
        )  # fmt: skip
        req = StructuredRequest(
            feature="drafting",
            system=(PROMPTS_DIR / "draft_system.md").read_text(encoding="utf-8").strip() + "\n",
            messages=[{"role": "user", "content": user.strip() + "\n"}],
            tool_name=TOOL_NAME,
            tool_description="Submit the finished outreach email (subject and plain-text body).",
            input_schema=_tool_schema(),
            max_tokens=900,
            prompt_version=PROMPT_VERSION,
            log_context={"enquiry_id": ref_enquiry, "input_summary": f"draft for {company[:60]}"},
        )
        call_at = now_or(now)
        try:
            resp = client.structured(req)
        except FATAL_LLM_ERRORS as exc:
            db.add(_error_row(exc, ref_enquiry, call_at))
            db.flush()
            raise
        except LLMBadResponse as exc:
            db.add(_error_row(exc, ref_enquiry, call_at))
            if attempt == 2:
                db.flush()
                raise DomainError("draft_failed", "The AI did not return a usable draft. Try again.", 502) from exc
            feedback = ["Your previous answer was not usable. Call write_email with subject and body."]
            continue
        except LLMError as exc:
            db.add(_error_row(exc, ref_enquiry, call_at))
            db.flush()
            raise DomainError("draft_failed", "The AI service is unavailable right now. Try again in a minute.", 502) from exc
        db.add(_llm_row(resp, ref_enquiry, call_at))
        data = resp.tool_input or {}
        subject, body = data.get("subject"), data.get("body")
        if not isinstance(subject, str) or not isinstance(body, str) or not body.strip():
            if attempt == 2:
                db.flush()
                raise DomainError("draft_failed", "The AI did not return a usable draft. Try again.", 502)
            feedback = ["Your previous answer had no subject/body. Call write_email with both."]
            continue
        subject = " ".join(subject.split())[:300]
        body = body.replace("\r\n", "\n").strip()
        if member.name.lower() not in body.lower():
            body = f"{body}\n\nBest regards,\n{member.name}"
        hard, soft = check_draft(subject, body, company=company, hooks=hooks, source_text=source_text, sender_name=member.name)
        if best is None or len(hard) <= len(best[2]):
            best = (subject, body, hard, soft)
        if not hard and not soft:
            break
        feedback = hard + soft
    assert best is not None
    subject, body, hard, soft = best

    conn = connection_status(db, member.id)
    to_list, cc_list = split_addrs(to), split_addrs(cc)
    flagged = hard + soft
    msg = OutreachMessage(
        member_id=member.id,
        connection_email=conn.email or member.email,
        from_addr=member.email,
        to_addrs=join_addrs(to_list),
        cc_addrs=join_addrs(cc_list),
        subject=subject,
        body=body,
        template_id=template.id if template else None,
        deck_id=deck.id if deck else None,
        state="draft",
        error=("Draft needs attention: " + " ".join(flagged))[:1000] if flagged else None,
        enquiry_id=enquiry_id,
        prospect_id=prospect_id,
        company_id=company_id,
        brief_json=json.dumps(brief, ensure_ascii=False) if brief else None,
        created_at=stamp,
        updated_at=stamp,
    )  # fmt: skip
    db.add(msg)
    db.flush()
    add_event(db, msg.id, "drafted", member.id, f"template={msg.template_id} deck={msg.deck_id} words={word_count(body)}", stamp)
    db.flush()
    log.info("draft_generated", message_id=msg.id, member_id=member.id, words=word_count(body), flagged=bool(flagged))
    return msg

