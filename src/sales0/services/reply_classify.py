"""Classify an inbound message: bounce / auto-reply / human reply, plus the human's intent.

Strong header evidence decides without any LLM call (bounces from MAILER-DAEMON or with
multipart/report, Auto-Submitted, X-Autoreply, Precedence: bulk|auto_reply|junk, "Automatic
reply" subjects, no-reply senders). Everything else goes to Haiku (feature ``triage``,
prompt ``reply-v1``), which sees only sender, subject and Gmail's snippet and answers
through a forced tool call. The text is untrusted: the answer is validated against fixed enums.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.models import LlmCall
from sales0.db.models.replies import REPLY_INTENTS, REPLY_KINDS
from sales0.integrations.gmail.threads import ThreadMessage
from sales0.llm import LLMAuthError, LLMBadResponse, LLMClient, LLMError, LLMModelNotFound, get_llm_client
from sales0.llm.client import StructuredRequest
from sales0.services._common import now_or

PROMPT_VERSION = "reply-v1"
TOOL_NAME = "record_reply"
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
FATAL_LLM_ERRORS = (LLMAuthError, LLMModelNotFound)

_BOUNCE_SENDERS = re.compile(r"^(mailer-daemon|postmaster|mail-daemon|mailerdaemon)(?![\w-])", re.I)
_SOFT_BOUNCE = re.compile(
    r"delay|temporar|try again|deferred|mailbox (is )?full|over quota|quota exceeded|still trying|will keep trying", re.I
)
_AUTO_SUBJECT = re.compile(
    r"^\s*(re:\s*)?(automatic reply|auto[- ]?reply|autoreply|auto:|out of office|out-of-office|ooo\b|away from (the )?office|"
    r"i am out of (the )?office|vacation reply)",
    re.I,
)
_NOREPLY_SENDER = re.compile(r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|noreply)", re.I)


@dataclass(frozen=True)
class Classification:
    kind: str  # human | auto_reply | bounce
    intent: str | None
    reason: str
    by: str  # headers | llm | fallback
    hard_bounce: bool = True


def classify_by_headers(msg: ThreadMessage) -> Classification | None:
    """A decision from headers alone, or None when the message is ambiguous (ask the LLM)."""
    local = msg.from_addr.split("@", 1)[0]
    ctype = msg.content_type.lower()
    is_dsn = (
        "multipart/report" in ctype
        or "delivery-status" in ctype
        or bool(msg.x_failed_recipients)
        or bool(_BOUNCE_SENDERS.match(local))
    )
    if is_dsn:
        soft = bool(_SOFT_BOUNCE.search(msg.snippet) or re.search(r"\(delay\)", msg.subject, re.I))
        return Classification("bounce", None, "delivery status notification" + (" (temporary)" if soft else ""), "headers", hard_bounce=not soft)
    auto = msg.auto_submitted.strip().lower()
    if auto and auto != "no":
        return Classification("auto_reply", None, f"Auto-Submitted: {auto}"[:300], "headers")
    if msg.x_autoreply:
        return Classification("auto_reply", None, "X-Autoreply header", "headers")
    if msg.precedence.strip().lower() in {"auto_reply", "auto-reply", "bulk", "junk", "list"}:
        return Classification("auto_reply", None, f"Precedence: {msg.precedence.strip().lower()}", "headers")
    if _AUTO_SUBJECT.search(msg.subject):
        return Classification("auto_reply", None, "automatic-reply subject", "headers")
    if _NOREPLY_SENDER.match(local):
        return Classification("auto_reply", None, "no-reply sender", "headers")
    return None


def _tool_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(REPLY_KINDS)},
            "intent": {"type": "string", "enum": list(REPLY_INTENTS)},
            "reason": {"type": "string", "description": "one short line, under 140 characters"},
        },
        "required": ["kind", "intent", "reason"],
    }


def _llm_row(resp: object | None, now: datetime, exc: Exception | None = None) -> LlmCall:
    if resp is None:
        return LlmCall(
            feature="triage", model=get_settings().llm_model, prompt_version=PROMPT_VERSION, status="error",
            error_type=str(getattr(exc, "error_type", type(exc).__name__))[:100], created_at=now,
        )  # fmt: skip
    return LlmCall(
        feature="triage", model=resp.model[:100], prompt_version=PROMPT_VERSION,  # type: ignore[attr-defined]
        input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,  # type: ignore[attr-defined]
        cost_usd_micros=resp.cost_usd_micros, latency_ms=resp.latency_ms, status="ok",  # type: ignore[attr-defined]
        request_id=resp.request_id[:100] if resp.request_id else None, created_at=now,  # type: ignore[attr-defined]
    )  # fmt: skip


def _inert(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def classify_with_llm(db: Session, llm: LLMClient | None, msg: ThreadMessage, *, now: datetime | None = None) -> Classification:
    """Ask Haiku. Raises FATAL LLM errors and LLMError (transient) for the caller to handle;
    a malformed answer raises LLMBadResponse. Adds an ``llm_calls`` row either way."""
    client = llm or get_llm_client()
    user = (
        f"From: {_inert(msg.from_addr, 120)}\nSubject: {_inert(msg.subject, 200)}\n"
        f"Snippet: {_inert(msg.snippet, 500)}\n"
    )
    req = StructuredRequest(
        feature="triage",
        system=(PROMPTS_DIR / "reply_classify_system.md").read_text(encoding="utf-8").strip() + "\n",
        messages=[{"role": "user", "content": user}],
        tool_name=TOOL_NAME,
        tool_description="Record the classification of the inbound email.",
        input_schema=_tool_schema(),
        max_tokens=300,
        prompt_version=PROMPT_VERSION,
        log_context={"input_summary": "reply classification"},
    )
    stamp = now_or(now)
    try:
        resp = client.structured(req)
    except LLMError as exc:
        db.add(_llm_row(None, stamp, exc))
        db.flush()
        raise
    db.add(_llm_row(resp, stamp))
    data = resp.tool_input or {}
    kind, intent, reason = data.get("kind"), data.get("intent"), data.get("reason")
    if kind not in REPLY_KINDS or intent not in REPLY_INTENTS:
        db.flush()
        raise LLMBadResponse("reply classification was not a valid kind/intent")
    db.flush()
    return Classification(
        kind=kind, intent=intent if kind == "human" else None,
        reason=_inert(str(reason or ""), 300) or "classified by AI", by="llm",
        hard_bounce=not _SOFT_BOUNCE.search(msg.snippet) if kind == "bounce" else True,
    )  # fmt: skip


__all__ = ["FATAL_LLM_ERRORS", "PROMPT_VERSION", "Classification", "classify_by_headers", "classify_with_llm"]
