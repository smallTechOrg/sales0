"""Triage graph assembly + routing (agent.md -> Capability 1 -> Edges)."""

from __future__ import annotations

from sales0.agent.nodes import (
    MAX_ATTEMPTS,
    PROMPT_VERSION,
    build_prompt,
    call_model,
    finalize,
    repair,
    validate_and_guard,
)
from sales0.agent.schemas import TriageDeps, TriageInput, TriageOutcome
from sales0.agent.state import TriageState


def run_triage(inp: TriageInput, deps: TriageDeps) -> TriageOutcome:
    """One structured call (+ at most one repair). Raises LLMAuthError / LLMModelNotFound (fatal)."""
    s = TriageState(inp, list(deps.offerings), list(deps.examples), PROMPT_VERSION, criteria=deps.criteria)
    s = build_prompt(s, deps)
    while True:
        s = call_model(s, deps)  # fatal errors propagate
        if s.error:
            break
        s = validate_and_guard(s, deps)
        if s.result or s.attempts >= MAX_ATTEMPTS:
            break
        s = repair(s, deps)
    return finalize(s, deps)
