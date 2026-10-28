"""Phase 3 company-research agent: bounded ReAct loop over a safe, same-site page fetcher."""

from __future__ import annotations

from sales0.agent.research.fetch import FetchError, SafeFetcher, is_blocked_ip
from sales0.agent.research.loop import PROMPT_VERSION, run_research
from sales0.agent.research.schemas import (
    CompanyBrief,
    ResearchLimits,
    ResearchResult,
    StepRecord,
)

__all__ = [
    "PROMPT_VERSION",
    "CompanyBrief",
    "FetchError",
    "ResearchLimits",
    "ResearchResult",
    "SafeFetcher",
    "StepRecord",
    "is_blocked_ip",
    "run_research",
]
