"""Real-Haiku helpers for the agent integration tests (key from .env; no fakes on this path)."""

from __future__ import annotations

from sales0.agent.graph import run_triage
from sales0.agent.schemas import (
    DecisionExample,
    OfferingRef,
    TriageDeps,
    TriageInput,
    TriageOutcome,
)
from sales0.config import get_settings
from sales0.llm import get_llm_client
from sales0.llm.pricing import cost_usd_micros
from tests.integration.agent.corpus import CORPUS, SEED_OFFERINGS

# One real call per corpus post per session: every expectation test reads the same outcome.
_CORPUS_OUTCOMES: dict[str, TriageOutcome] = {}


def real_deps(
    offerings: list[OfferingRef] | None = None, examples: list[DecisionExample] | None = None
) -> TriageDeps:
    return TriageDeps(
        llm=get_llm_client(),
        offerings=SEED_OFFERINGS if offerings is None else offerings,
        examples=examples or [],
        settings=get_settings(),
    )


def triage_real(inp: TriageInput, **kwargs) -> TriageOutcome:
    return run_triage(inp, real_deps(**kwargs))


def corpus_outcome(post_id: str) -> TriageOutcome:
    if post_id not in _CORPUS_OUTCOMES:
        _CORPUS_OUTCOMES[post_id] = triage_real(CORPUS[post_id])
    return _CORPUS_OUTCOMES[post_id]


def assert_well_formed(outcome: TriageOutcome) -> None:
    """Structure every successful real triage must have (never prose)."""
    assert outcome.status == "done", outcome.error
    assert outcome.result is not None
    r = outcome.result
    assert 1 <= len(r.reason) <= 160 and "\n" not in r.reason
    assert 1 <= len(r.requirement_summary) <= 140 and "\n" not in r.requirement_summary
    if r.verdict == "discard":
        assert r.priority == "low"
    assert 1 <= len(outcome.llm_responses) <= 2
    settings = get_settings()
    for resp in outcome.llm_responses:
        assert resp.input_tokens > 0 and resp.output_tokens > 0
        assert resp.cost_usd_micros == cost_usd_micros(resp.input_tokens, resp.output_tokens, settings) > 0
        assert resp.model.startswith(settings.llm_model)
    assert outcome.prompt_version == "triage-v1"
