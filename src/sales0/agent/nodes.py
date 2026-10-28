"""Triage graph nodes (agent.md -> Capability 1 -> Nodes). Each is ``node(state, deps) -> state``."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import ValidationError

from sales0.agent.fewshot import truncate_words
from sales0.agent.guards import apply_guards
from sales0.agent.schemas import (
    TriageDeps,
    TriageOutcome,
    TriageResult,
    triage_tool_schema,
)
from sales0.agent.state import TriageState
from sales0.llm.client import StructuredRequest
from sales0.llm.errors import (
    LLMAuthError,
    LLMError,
    LLMModelNotFound,
    LLMTransientError,
)
from sales0.logging import get_logger

log = get_logger("sales0.agent")

PROMPT_VERSION = "triage-v3"  # bump on any prompt or schema edit
TOOL_NAME = "record_triage"
TOOL_DESCRIPTION = (
    "Record the triage of ONE post: genuine/discard verdict, one-line reason, poster type, "
    "requirement summary, priority, best-matching offering id, and contact/requirement details "
    "copied from the post (null when absent)."
)
MAX_ATTEMPTS = 2
INVALID_TWICE_MESSAGE = "AI returned an invalid answer twice"
UNAVAILABLE_MESSAGE = "LLM unavailable — retry later"
REJECTED_MESSAGE = "AI request rejected — see logs"  # provider detail goes to the log only
REPAIR_TEMPLATE = (
    "Your record_triage input was invalid: {error}. Call record_triage again with corrected fields."
)
NO_TOOL_USE_ERROR = "no record_triage tool call in the answer"
_REPAIR_TOOL_USE_ID = "toolu_triage_attempt_{n}"
_MAX_ERROR_CHARS = 300  # enquiries.triage_error is str(300)

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


def inert(value: object) -> str:
    """Neutralise delimiter breakouts in untrusted text: ``<``/``>`` -> ``&lt;``/``&gt;``."""
    return str(value).replace("<", "&lt;").replace(">", "&gt;")


@lru_cache(maxsize=1)
def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(PROMPTS_DIR),
        autoescape=False,  # plain-text prompts, not HTML; post text is neutralised by `inert`
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    env.filters["inert"] = inert
    return env


def render_system(state: TriageState) -> str:
    return _env().get_template("triage_system.md").render(offerings=state.offerings).strip() + "\n"


def render_user(state: TriageState, max_input_chars: int) -> str:
    inp = state.inp
    return (
        _env()
        .get_template("triage_user.md.j2")
        .render(
            examples=state.examples,
            author_name=inp.author_name,
            author_headline=inp.author_headline,
            date_posted=inp.date_posted.isoformat() if inp.date_posted else None,
            source_url=inp.source_url,
            text=truncate_words(inp.text, max_input_chars),
        )
        .strip()
    )


# ---------------------------------------------------------------------------- nodes
def build_prompt(state: TriageState, deps: TriageDeps) -> TriageState:
    settings = deps.settings
    state.request = StructuredRequest(
        feature="triage",
        system=render_system(state),
        messages=[{"role": "user", "content": render_user(state, settings.triage_max_input_chars)}],
        tool_name=TOOL_NAME,
        tool_description=TOOL_DESCRIPTION,
        input_schema=triage_tool_schema(),
        max_tokens=settings.llm_max_output_tokens,
        prompt_version=state.prompt_version,
        log_context={
            ("seen_post_id" if state.inp.subject == "seen_post" else "enquiry_id"): state.inp.enquiry_id,
            "fewshot_count": len(state.examples),
            "input_summary": " ".join(state.inp.text.split())[:80],
        },
    )
    return state


def call_model(state: TriageState, deps: TriageDeps) -> TriageState:
    """One structured request. Fatal auth/model errors propagate; other LLM errors fail the triage."""
    assert state.request is not None, "build_prompt must run first"
    state.attempts += 1
    try:
        response = deps.llm.structured(state.request)
    except (LLMAuthError, LLMModelNotFound):
        raise
    except LLMTransientError as exc:
        state.error = f"LLM unavailable: {exc.error_type}"
        return state
    except LLMError as exc:  # LLMBadResponse: the request itself was rejected
        log.warning(
            "triage_request_rejected",
            enquiry_id=state.inp.enquiry_id,
            error_type=exc.error_type,
            provider_message=exc.message[:_MAX_ERROR_CHARS],
        )
        state.error = REJECTED_MESSAGE
        return state
    state.responses.append(response)
    state.raw_tool_input = response.tool_input
    return state


def _compact_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:6]:
        path = ".".join(str(p) for p in err.get("loc", ())) or "input"
        parts.append(f"{path}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)


def validate_and_guard(state: TriageState, deps: TriageDeps) -> TriageState:
    state.result = None
    if state.raw_tool_input is None:
        state.validation_error = NO_TOOL_USE_ERROR
        return state
    try:
        parsed = TriageResult.model_validate(state.raw_tool_input)
    except ValidationError as exc:
        state.validation_error = _compact_validation_error(exc)
        return state
    state.validation_error = None
    state.result = apply_guards(parsed, state.inp, [o.id for o in state.offerings])
    return state


def repair(state: TriageState, deps: TriageDeps) -> TriageState:
    """Append the invalid tool call + an error tool_result so the next call can correct it.

    ``LLMResponse`` carries no tool_use id, so a deterministic id is synthesised; the
    Anthropic API accepts any well-formed id that the following tool_result references.
    """
    assert state.request is not None and state.validation_error is not None
    tool_use_id = _REPAIR_TOOL_USE_ID.format(n=state.attempts)
    tool_input: dict[str, Any] = state.raw_tool_input if isinstance(state.raw_tool_input, dict) else {}
    messages = [
        *state.request.messages,
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_use_id, "name": TOOL_NAME, "input": tool_input}],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "is_error": True,
                    "content": REPAIR_TEMPLATE.format(error=state.validation_error),
                }
            ],
        },
    ]
    state.request = state.request.model_copy(update={"messages": messages})
    return state


def finalize(state: TriageState, deps: TriageDeps) -> TriageOutcome:
    common = {
        "llm_responses": list(state.responses),
        "fewshot_count": len(state.examples),
        "prompt_version": state.prompt_version,
    }
    if state.error:
        # Transient: the generic retry message. A rejected request: a stable message (detail is logged).
        transient = state.error == f"LLM unavailable: {LLMTransientError.error_type}"
        message = UNAVAILABLE_MESSAGE if transient else state.error
        return TriageOutcome(status="failed", result=None, error=message[:_MAX_ERROR_CHARS], **common)
    if state.result is None:
        return TriageOutcome(status="failed", result=None, error=INVALID_TWICE_MESSAGE, **common)
    return TriageOutcome(status="done", result=state.result, error=None, **common)
