"""The triage graph's state (agent.md -> Capability 1 -> State)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sales0.agent.criteria import CriteriaSet
from sales0.agent.schemas import DecisionExample, OfferingRef, TriageInput, TriageResult
from sales0.llm.client import LLMResponse, StructuredRequest


@dataclass
class TriageState:
    inp: TriageInput
    offerings: list[OfferingRef]
    examples: list[DecisionExample]  # already selected + truncated
    prompt_version: str  # PROMPT_VERSION from nodes.py
    request: StructuredRequest | None = None
    raw_tool_input: dict[str, Any] | None = None
    result: TriageResult | None = None
    validation_error: str | None = None
    attempts: int = 0  # LLM requests made (max 2)
    responses: list[LLMResponse] = field(default_factory=list)
    error: str | None = None
    criteria: CriteriaSet | None = None  # None = the built-in default rules
