"""Proactive keyword suggestions (Phase 4)."""

from sales0.agent.suggest.loop import run_suggestions
from sales0.agent.suggest.rules import DEFAULT_MIN_SCANNED, dead_keywords, is_grounded
from sales0.agent.suggest.schemas import PROMPT_VERSION, SuggestionBatch, SuggestOutput

__all__ = [
    "DEFAULT_MIN_SCANNED",
    "PROMPT_VERSION",
    "SuggestOutput",
    "SuggestionBatch",
    "dead_keywords",
    "is_grounded",
    "run_suggestions",
]
