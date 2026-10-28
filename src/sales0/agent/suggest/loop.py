"""Proactive suggestions: ONE batched Haiku call (forced tool use, repaired once) proposes new
search keywords and phrases rationales for dead ones; code then enforces grounding, de-duplication
and the dead-keyword rule before anything is stored. Fatal LLM errors propagate after the error
row is flushed; the spend cap is checked before the call."""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sales0.agent.suggest.rules import (
    DEFAULT_MIN_SCANNED,
    dead_keywords,
    is_grounded,
    norm_phrase,
)
from sales0.agent.suggest.schemas import (
    PHRASE_MAX,
    PHRASE_MIN,
    PROMPT_VERSION,
    TOOL_NAME,
    SuggestionBatch,
    SuggestOutput,
    tool_schema,
)
from sales0.config import get_settings
from sales0.db.models import Enquiry, Keyword, LlmCall, Suggestion
from sales0.errors import DomainError
from sales0.llm import (
    LLMAuthError,
    LLMBadResponse,
    LLMClient,
    LLMError,
    LLMModelNotFound,
    get_llm_client,
)
from sales0.llm.client import StructuredRequest
from sales0.logging import get_logger
from sales0.services._common import clip, now_or
from sales0.services.costs_report import ensure_within_spend_cap
from sales0.services.keyword_yield import keyword_yield

log = get_logger("sales0.suggest")

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
FATAL_LLM_ERRORS = (LLMAuthError, LLMModelNotFound)
MIN_ENQUIRIES = 3
MAX_ENQUIRIES = 25
ENQUIRY_CLIP = 400
SNIPPET_CLIP = 200
MAX_PAUSE = 5


def _inert(value: object) -> str:
    return str(value).replace("<", "&lt;").replace(">", "&gt;")


@lru_cache(maxsize=1)
def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(PROMPTS_DIR),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    env.filters["inert"] = _inert
    return env


def render_user_prompt(
    *,
    yields: list[Any],
    dead: list[Any],
    existing: list[str],
    enquiries: list[str],
    max_new: int,
    feedback: list[str] | None = None,
) -> str:
    return _env().get_template("suggest_user.md.j2").render(
        yields=yields, dead=dead, existing=existing, enquiries=enquiries, max_new=max_new, feedback=feedback or []
    ).strip() + "\n"  # fmt: skip


def system_prompt() -> str:
    return (PROMPTS_DIR / "suggest_system.md").read_text(
        encoding="utf-8"
    ).strip() + "\n"


def _llm_row(resp: Any, now: datetime) -> LlmCall:
    return LlmCall(
        feature="suggest", model=resp.model[:100], prompt_version=PROMPT_VERSION,
        input_tokens=resp.input_tokens, output_tokens=resp.output_tokens, cost_usd_micros=resp.cost_usd_micros,
        latency_ms=resp.latency_ms, status="ok", request_id=resp.request_id[:100] if resp.request_id else None,
        created_at=now,
    )  # fmt: skip


def _error_row(exc: Exception, now: datetime) -> LlmCall:
    return LlmCall(
        feature="suggest", model=get_settings().llm_model, prompt_version=PROMPT_VERSION, status="error",
        error_type=str(getattr(exc, "error_type", type(exc).__name__))[:100], created_at=now,
    )  # fmt: skip


def _call(
    db: Session, client: LLMClient, user: str, batch: SuggestionBatch, now: datetime
) -> SuggestOutput | None:
    """One request; returns the validated output, or None if unusable (an ok/error row is recorded)."""
    req = StructuredRequest(
        feature="suggest", system=system_prompt(), messages=[{"role": "user", "content": user}],
        tool_name=TOOL_NAME, tool_description="Record the new keyword suggestions and pause rationales.",
        input_schema=tool_schema(), max_tokens=1500, prompt_version=PROMPT_VERSION,
        log_context={"input_summary": "keyword suggestions"},
    )  # fmt: skip
    try:
        resp = client.structured(req)
    except FATAL_LLM_ERRORS as exc:
        db.add(_error_row(exc, now))
        db.flush()
        raise
    except LLMBadResponse as exc:
        db.add(_error_row(exc, now))
        batch.llm_calls += 1
        return None
    except LLMError as exc:
        db.add(_error_row(exc, now))
        db.flush()
        raise DomainError(
            "suggest_failed",
            "The AI service is unavailable right now. Try again in a minute.",
            502,
        ) from exc
    db.add(_llm_row(resp, now))
    batch.llm_calls += 1
    batch.cost_usd_micros += resp.cost_usd_micros
    if resp.tool_input is None:
        return None
    try:
        return SuggestOutput.model_validate(resp.tool_input)
    except ValidationError:
        return None


def _evidence_json(indexes: list[int], enquiry_ids: list[int], texts: list[str]) -> str:
    items = [
        {"enquiry_id": enquiry_ids[i - 1], "snippet": clip(texts[i - 1], SNIPPET_CLIP)}
        for i in dict.fromkeys(indexes)
        if 1 <= i <= len(texts)
    ]
    return json.dumps(items, ensure_ascii=False)


def _insert(db: Session, row: Suggestion) -> bool:
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:  # lost a race against the unique dedupe_key
        return False
    return True


def run_suggestions(
    db: Session,
    llm: LLMClient | None = None,
    now: datetime | None = None,
    *,
    max_new: int = 5,
    min_scanned: int = DEFAULT_MIN_SCANNED,
) -> SuggestionBatch:
    """Generate and store new pending suggestions (flushed; the caller commits)."""
    stamp = now_or(now)
    batch = SuggestionBatch()
    rows = db.execute(
        select(Enquiry.id, Enquiry.raw_text)
        .where(Enquiry.ai_verdict == "genuine")
        .order_by(Enquiry.date_added.desc(), Enquiry.id.desc())
        .limit(MAX_ENQUIRIES)
    ).all()  # fmt: skip
    if len(rows) < MIN_ENQUIRIES:
        batch.message = (
            f"Not enough data yet: {len(rows)} genuine enquir{'y' if len(rows) == 1 else 'ies'} found, "
            f"at least {MIN_ENQUIRIES} are needed. Run a search first."
        )
        return batch
    ensure_within_spend_cap(db, stamp)  # raises SpendCapReached before any LLM call

    enquiry_ids = [r[0] for r in rows]
    texts = [clip(" ".join((r[1] or "").split()), ENQUIRY_CLIP) or "" for r in rows]
    keywords = list(db.scalars(select(Keyword).order_by(Keyword.id)))
    existing_norm = {norm_phrase(k.text) for k in keywords}
    yields = keyword_yield(db)
    dead = dead_keywords(yields, min_scanned)
    dead_by_id = {y.keyword_id: y for y in dead}

    client = llm or get_llm_client()
    output: SuggestOutput | None = None
    feedback: list[str] = []
    for _attempt in (1, 2):
        user = render_user_prompt(
            yields=yields, dead=dead, existing=[k.text for k in keywords], enquiries=texts, max_new=max_new, feedback=feedback
        )  # fmt: skip
        batch.llm_called = True
        output = _call(db, client, user, batch, stamp)
        if output is not None:
            break
        feedback = [
            f"Your previous answer was not valid. Call {TOOL_NAME} with new_keywords and pause_keywords; phrases are {PHRASE_MIN}-{PHRASE_MAX} characters."
        ]
    if output is None:
        db.flush()
        raise DomainError(
            "suggest_failed",
            "The AI did not return usable suggestions. Try again.",
            502,
        )

    stored = {k for (k,) in db.execute(select(Suggestion.dedupe_key))}
    new_added = 0
    for item in output.new_keywords:
        phrase = item.phrase
        key = f"new_keyword:{norm_phrase(phrase)}"
        if new_added >= max_new:
            batch.dropped.append(f"{phrase}: over the limit")
            continue
        if norm_phrase(phrase) in existing_norm:
            batch.dropped.append(f"{phrase}: already a keyword")
            continue
        if key in stored:
            batch.dropped.append(f"{phrase}: already suggested")
            continue
        problem = is_grounded(phrase, item.evidence, texts)
        if problem:
            batch.dropped.append(f"{phrase}: {problem}")
            continue
        row = Suggestion(
            kind="new_keyword", text=phrase, dedupe_key=key, rationale=item.rationale, status="pending",
            evidence_json=_evidence_json(item.evidence, enquiry_ids, texts), prompt_version=PROMPT_VERSION, created_at=stamp,
        )  # fmt: skip
        if _insert(db, row):
            stored.add(key)
            batch.created_ids.append(row.id)
            new_added += 1
        else:
            batch.dropped.append(f"{phrase}: already suggested")
    batch.new_count = new_added

    pause_added = 0
    said = {p.keyword_id: p.rationale for p in output.pause_keywords}
    for pid in said.keys() - dead_by_id.keys():
        batch.dropped.append(f"pause #{pid}: the data does not show it dead")
    for y in dead:  # code decides WHAT to pause; the model only phrases why
        key = f"pause_keyword:{norm_phrase(y.text)}"
        if key in stored or pause_added >= MAX_PAUSE:
            continue
        why = (
            said.get(y.keyword_id)
            or f"Scanned {y.scanned} posts across {y.runs} run(s) without a single genuine enquiry."
        )
        row = Suggestion(
            kind="pause_keyword", text=y.text, keyword_id=y.keyword_id, dedupe_key=key, rationale=why, status="pending",
            evidence_json=json.dumps({"scanned": y.scanned, "relevant": y.relevant, "runs": y.runs}),
            prompt_version=PROMPT_VERSION, created_at=stamp,
        )  # fmt: skip
        if _insert(db, row):
            stored.add(key)
            batch.created_ids.append(row.id)
            pause_added += 1
    batch.pause_count = pause_added
    db.flush()
    batch.message = (
        f"{batch.created} new suggestion(s): {batch.new_count} keyword(s) to add, {batch.pause_count} to pause."
        if batch.created
        else "No new suggestions this time - nothing new that the data supports."
    )
    log.info(
        "suggestions_generated",
        created=batch.created,
        dropped=len(batch.dropped),
        cost_usd_micros=batch.cost_usd_micros,
    )
    return batch
