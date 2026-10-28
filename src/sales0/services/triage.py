"""Triage persistence: run the agent on one enquiry and store its outcome.

The agent (``sales0.agent``) is DB-free apart from the read-only few-shot loader; this
module writes the ``ai_*`` fields, initialises the editable fields, inserts one
``llm_calls`` row per Anthropic response and one ``status_history`` row.

``LLMAuthError`` / ``LLMModelNotFound`` are fatal: an ``llm_calls`` error row is added to
the session and the exception propagates (the caller decides whether to commit).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.agent import (
    OfferingRef,
    TriageDeps,
    TriageInput,
    TriageOutcome,
    load_candidates,
    run_triage,
    select_examples,
)
from sales0.agent.nodes import PROMPT_VERSION
from sales0.config import get_settings
from sales0.db.models import Enquiry, LlmCall, Offering, OfferingGroup
from sales0.llm import LLMAuthError, LLMClient, LLMModelNotFound, get_llm_client
from sales0.services._common import clip, now_or, not_found, record_history

FATAL_LLM_ERRORS = (LLMAuthError, LLMModelNotFound)


def active_offering_refs(db: Session) -> list[OfferingRef]:
    rows = db.execute(
        select(Offering, OfferingGroup.name)
        .join(OfferingGroup, OfferingGroup.id == Offering.group_id)
        .where(Offering.active.is_(True))
        .order_by(OfferingGroup.sort_order, OfferingGroup.id, Offering.sort_order, Offering.id)
    ).all()
    return [OfferingRef(id=o.id, name=o.name, group=g, description=o.description or "") for o, g in rows]


def _error_row(
    db: Session, enquiry_id: int, run_id: int | None, exc: Exception, fewshot_count: int, now: datetime
) -> None:
    settings = get_settings()
    db.add(
        LlmCall(
            feature="triage",
            model=settings.llm_model,
            prompt_version=PROMPT_VERSION,
            enquiry_id=enquiry_id,
            run_id=run_id,
            input_tokens=0,
            output_tokens=0,
            cost_usd_micros=0,
            latency_ms=0,
            fewshot_count=fewshot_count,
            status="error",
            error_type=getattr(exc, "error_type", type(exc).__name__)[:100],
            created_at=now,
        )
    )


def triage_enquiry(
    db: Session,
    enquiry_id: int,
    llm: LLMClient | None = None,
    *,
    run_id: int | None = None,
    now: datetime | None = None,
) -> TriageOutcome:
    """One triage (one call + at most one repair). Flushes; the caller commits."""
    settings = get_settings()
    enquiry = db.get(Enquiry, enquiry_id)
    if enquiry is None:
        raise not_found()

    offerings = active_offering_refs(db)
    candidates = load_candidates(db, exclude_enquiry_id=enquiry_id)
    examples = select_examples(candidates, settings.fewshot_max_examples, settings.fewshot_text_chars)
    inp = TriageInput(
        enquiry_id=enquiry.id,
        text=enquiry.raw_text,
        author_name=enquiry.author_name,
        author_headline=enquiry.author_headline,
        source_url=enquiry.source_url,
        date_posted=enquiry.date_posted,
    )

    try:
        client = llm or get_llm_client()
        outcome = run_triage(inp, TriageDeps(llm=client, offerings=offerings, examples=examples, settings=settings))
    except FATAL_LLM_ERRORS as exc:
        _error_row(db, enquiry_id, run_id, exc, len(examples), now_or(now))
        db.flush()
        raise

    stamp = now_or(now)
    for resp in outcome.llm_responses:
        db.add(
            LlmCall(
                feature="triage",
                model=resp.model[:100],
                prompt_version=outcome.prompt_version,
                enquiry_id=enquiry_id,
                run_id=run_id,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                cost_usd_micros=resp.cost_usd_micros,
                latency_ms=resp.latency_ms,
                fewshot_count=outcome.fewshot_count,
                status="ok",
                error_type=None,
                request_id=resp.request_id[:100] if resp.request_id else None,
                created_at=stamp,
            )
        )
    if outcome.status == "failed" and not outcome.llm_responses:
        # The request never produced a response (transient / rejected): keep it observable.
        db.add(
            LlmCall(
                feature="triage",
                model=settings.llm_model,
                prompt_version=outcome.prompt_version,
                enquiry_id=enquiry_id,
                run_id=run_id,
                fewshot_count=outcome.fewshot_count,
                status="error",
                error_type=clip(outcome.error, 100),
                created_at=stamp,
            )
        )

    previous = enquiry.triage_status
    enquiry.prompt_version = outcome.prompt_version
    enquiry.triaged_at = stamp
    if outcome.status == "done" and outcome.result is not None:
        r = outcome.result
        enquiry.triage_status = "done"
        enquiry.triage_error = None
        enquiry.ai_verdict = r.verdict
        enquiry.ai_reason = clip(r.reason, 160) or "No reason given"
        enquiry.ai_poster_type = r.poster_type
        enquiry.ai_opportunity_type = r.opportunity_type
        enquiry.ai_priority = r.priority
        enquiry.ai_offering_id = r.offering_id
        enquiry.ai_extraction = r.model_dump(mode="json")
        enquiry.requirement_summary = clip(r.requirement_summary, 140)
        enquiry.priority = r.priority
        enquiry.offering_id = r.offering_id
        enquiry.company = clip(r.company, 200)
        enquiry.contact_person = clip(r.contact_person, 200)
        enquiry.email = clip(r.email, 254)
        enquiry.phone = clip(r.phone, 50)
        enquiry.city = clip(r.city, 100)
        enquiry.mode = r.mode
        enquiry.dates_mentioned = clip(r.dates_mentioned, 300)
        enquiry.topic = clip(r.topic, 300)
    else:
        enquiry.triage_status = "failed"
        enquiry.triage_error = clip(outcome.error or "AI triage failed", 300)
    record_history(
        db,
        "enquiry",
        enquiry.id,
        "triage_status",
        previous,
        enquiry.triage_status,
        note=enquiry.triage_error,
        now=stamp,
    )
    db.flush()
    return outcome


__all__ = ["FATAL_LLM_ERRORS", "active_offering_refs", "triage_enquiry"]
