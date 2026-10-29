"""AI account summary of one prospect: a SINGLE Haiku call (forced tool use) over everything we know.

Inputs are assembled by ``services.research.context`` (our CRM data: enquiries / posts, contacts and headlines, the
latest research, our notes, outreach and replies, follow-ups and timeline, sheet data). Third-party text inside them
is untrusted DATA. The answer is structured (``record_account_summary``) and rendered to plain text, which the team
can then edit (``services.research.docs``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from sales0.agent.research.loop import FATAL_LLM_ERRORS, PROMPTS_DIR, _env
from sales0.config import get_settings
from sales0.db.models import LlmCall
from sales0.llm import LLMBadResponse, LLMClient, LLMError, get_llm_client
from sales0.llm.client import StructuredRequest

PROMPT_VERSION = "summary-v1"
TOOL_NAME = "record_account_summary"
MAX_INPUT_CHARS = 24_000

SECTIONS: tuple[tuple[str, str], ...] = (
    ("who_they_are", "Who they are"),
    ("what_they_want", "What they want from us"),
    ("where_we_stand", "Where we stand"),
    ("characteristics", "Characteristics"),
    ("open_follow_ups", "Open follow-ups"),
    ("next_best_step", "Next best step"),
)


class AccountSummary(BaseModel):
    who_they_are: str = Field(min_length=1, max_length=800)
    what_they_want: str = Field(default="unknown", max_length=800)
    where_we_stand: str = Field(default="unknown", max_length=800)
    characteristics: list[str] = Field(default_factory=list, max_length=8)
    open_follow_ups: list[str] = Field(default_factory=list, max_length=8)
    next_best_step: str = Field(default="unknown", max_length=500)

    @field_validator("characteristics", "open_follow_ups", mode="before")
    @classmethod
    def _clean(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        if isinstance(v, list):
            return [" ".join(str(x).split())[:300] for x in v if str(x).strip()][:8]
        return v


def tool_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "who_they_are": {"type": "string", "description": "1-2 sentences: the company / person, sector, size hints"},
            "what_they_want": {"type": "string", "description": "what they asked for or are likely to need from smallTech, or 'unknown'"},
            "where_we_stand": {"type": "string", "description": "relationship status: stage, contacts made, emails sent, replies, lead / client status"},
            "characteristics": {"type": "array", "items": {"type": "string"}, "description": "e.g. Training provider, Partner, Outreach client (only from the records)"},
            "open_follow_ups": {"type": "array", "items": {"type": "string"}, "description": "open reminders / promises with their dates"},
            "next_best_step": {"type": "string", "description": "one concrete next action for the salesperson"},
        },
        "required": ["who_they_are", "what_they_want", "where_we_stand", "characteristics", "open_follow_ups", "next_best_step"],
    }  # fmt: skip


def render_text(summary: AccountSummary) -> str:
    """The editable plain-text form (headings + lines). Stored as the version's ``body``."""
    data = summary.model_dump()
    out: list[str] = []
    for key, label in SECTIONS:
        value = data[key]
        if isinstance(value, list):
            out.append(f"{label}:")
            out.extend(f"- {v}" for v in (value or ["none"]))
        else:
            out.append(f"{label}: {value}")
    return "\n".join(out)


@dataclass
class SummaryResult:
    status: str  # done | failed
    summary: AccountSummary | None = None
    text: str = ""
    cost_usd_micros: int = 0
    llm_calls: int = 0
    error: str | None = None
    responses: list[Any] = field(default_factory=list)


def _row(resp: Any, now: datetime) -> LlmCall:
    return LlmCall(
        feature="summary", model=resp.model[:100], prompt_version=PROMPT_VERSION, input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens, cost_usd_micros=resp.cost_usd_micros, latency_ms=resp.latency_ms, status="ok",
        request_id=resp.request_id[:100] if resp.request_id else None, created_at=now,
    )  # fmt: skip


def _error_row(exc: Exception, now: datetime) -> LlmCall:
    return LlmCall(
        feature="summary", model=get_settings().llm_model, prompt_version=PROMPT_VERSION, status="error",
        error_type=str(getattr(exc, "error_type", type(exc).__name__))[:100], created_at=now,
    )  # fmt: skip


def summarise_account(db: Session, llm: LLMClient | None, *, name: str, records: str) -> SummaryResult:
    """ONE request (no repair call: an unusable answer is a failed result the user can retry). Flushes.

    ``LLMAuthError`` / ``LLMModelNotFound`` propagate after adding an error row."""
    client = llm or get_llm_client()
    user = _env().get_template("summary_user.md.j2").render(name=name, records=records[:MAX_INPUT_CHARS])
    req = StructuredRequest(
        feature="summary",
        system=(PROMPTS_DIR / "summary_system.md").read_text(encoding="utf-8").strip() + "\n",
        messages=[{"role": "user", "content": user.strip() + "\n"}],
        tool_name=TOOL_NAME,
        tool_description="Record the account summary of this prospect.",
        input_schema=tool_schema(),
        max_tokens=1000,
        prompt_version=PROMPT_VERSION,
        log_context={"input_summary": f"summary {name[:60]}"},
    )
    now = datetime.now(UTC)
    try:
        resp = client.structured(req)
    except FATAL_LLM_ERRORS as exc:
        db.add(_error_row(exc, now))
        db.flush()
        raise
    except (LLMBadResponse, LLMError) as exc:
        db.add(_error_row(exc, now))
        db.flush()
        return SummaryResult(status="failed", llm_calls=1, error="The AI service is unavailable right now. Try again in a minute.")
    db.add(_row(resp, now))
    db.flush()
    try:
        if resp.tool_input is None:
            raise ValueError("no record_account_summary call")
        summary = AccountSummary.model_validate(resp.tool_input)
    except (ValidationError, ValueError):
        return SummaryResult(
            status="failed", cost_usd_micros=resp.cost_usd_micros, llm_calls=1,
            error="The AI did not return a usable summary. Try again.",
        )  # fmt: skip
    return SummaryResult(
        status="done", summary=summary, text=render_text(summary), cost_usd_micros=resp.cost_usd_micros, llm_calls=1
    )


__all__ = ["PROMPT_VERSION", "AccountSummary", "SummaryResult", "render_text", "summarise_account"]
