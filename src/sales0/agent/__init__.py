"""Triage agent: one structured Haiku call per enquiry + guards + repair + few-shot learning."""

from __future__ import annotations

from sales0.agent.fewshot import load_candidates, select_examples
from sales0.agent.graph import run_triage
from sales0.agent.nodes import PROMPT_VERSION
from sales0.agent.schemas import (
    DecisionExample,
    OfferingRef,
    TriageDeps,
    TriageInput,
    TriageOutcome,
    TriageResult,
)

__all__ = [
    "PROMPT_VERSION",
    "DecisionExample",
    "OfferingRef",
    "TriageDeps",
    "TriageInput",
    "TriageOutcome",
    "TriageResult",
    "load_candidates",
    "run_triage",
    "select_examples",
]
