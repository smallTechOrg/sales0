"""Research agent data types: limits, brief, per-step record, result."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def _env_num(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class ResearchLimits:
    """Hard caps. Defaults can be overridden via SALES0_RESEARCH_* env vars (integrator may wire Settings later)."""

    max_steps: int = 6
    max_pages: int = 5
    max_bytes_per_page: int = 200_000
    total_time_s: float = 60.0
    fetch_timeout_s: float = 10.0
    page_chars_to_model: int = 8_000
    max_links_to_model: int = 25
    max_tokens: int = 1_500

    @classmethod
    def from_env(cls) -> ResearchLimits:
        d = cls()
        return cls(
            max_steps=int(_env_num("SALES0_RESEARCH_MAX_STEPS", d.max_steps)),
            max_pages=int(_env_num("SALES0_RESEARCH_MAX_PAGES", d.max_pages)),
            max_bytes_per_page=int(
                _env_num("SALES0_RESEARCH_MAX_BYTES_PER_PAGE", d.max_bytes_per_page)
            ),
            total_time_s=_env_num("SALES0_RESEARCH_TOTAL_TIME_S", d.total_time_s),
            fetch_timeout_s=_env_num(
                "SALES0_RESEARCH_FETCH_TIMEOUT_S", d.fetch_timeout_s
            ),
        )


class CompanyBrief(BaseModel):
    company_name: str = Field(min_length=1, max_length=200)
    what_they_do: str = Field(min_length=1, max_length=1200)
    size_hints: str = Field(default="unknown", max_length=600)
    ai_training_needs: list[str] = Field(default_factory=list, max_length=6)
    suggested_offering: str = Field(default="unknown", max_length=600)
    matched_offering_name: str | None = Field(default=None, max_length=200)
    personalisation_hooks: list[str] = Field(default_factory=list, max_length=4)
    sources: list[str] = Field(default_factory=list)
    limited_information: bool = False

    @field_validator(
        "ai_training_needs", "personalisation_hooks", "sources", mode="before"
    )
    @classmethod
    def _clean_list(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return v


def brief_tool_schema() -> dict[str, Any]:
    """Hand-written JSON schema (no $defs) for the `brief` argument of the finish action."""
    return {
        "type": "object",
        "properties": {
            "company_name": {"type": "string"},
            "what_they_do": {"type": "string", "description": "2-4 sentences, only facts seen in fetched pages, else 'unknown'"},
            "size_hints": {"type": "string", "description": "headcount/offices/clients/revenue hints, or 'unknown'"},
            "ai_training_needs": {"type": "array", "items": {"type": "string"}, "description": "plausible AI-training needs grounded in what they do"},
            "suggested_offering": {"type": "string", "description": "which smallTech offering fits and why"},
            "matched_offering_name": {"type": ["string", "null"], "description": "exact name from the offerings list, or null"},
            "personalisation_hooks": {"type": "array", "items": {"type": "string"}, "description": "2-3 specific, page-backed details to personalise outreach"},
            "sources": {"type": "array", "items": {"type": "string"}, "description": "URLs of fetched pages that back the claims"},
        },
        "required": ["company_name", "what_they_do", "size_hints", "ai_training_needs", "suggested_offering", "personalisation_hooks", "sources"],
    }  # fmt: skip


def action_tool_schema(*, allow_fetch: bool) -> dict[str, Any]:
    actions = ["fetch_url", "finish"] if allow_fetch else ["finish"]
    return {
        "type": "object",
        "properties": {
            "thought": {"type": "string", "description": "one short sentence on why this action"},
            "action": {"type": "string", "enum": actions},
            "url": {"type": ["string", "null"], "description": "required for fetch_url: an absolute URL on the company's own site"},
            "brief": {**brief_tool_schema(), "description": "required for finish"},
        },
        "required": ["action"],
    }  # fmt: skip


class ResearchAction(BaseModel):
    thought: str | None = None
    action: Literal["fetch_url", "finish"]
    url: str | None = None
    brief: CompanyBrief | None = None


@dataclass
class StepRecord:
    index: int
    action: str  # fetch_url | finish | invalid | error
    url: str | None = None
    observation: str = ""  # short, for logs/UI (never the full page)
    ok: bool = True


@dataclass
class ResearchResult:
    status: Literal["done", "partial", "failed"]
    brief: CompanyBrief
    sources: list[str]
    steps: list[StepRecord] = field(default_factory=list)
    cost_usd_micros: int = 0
    llm_calls: int = 0
    pages_fetched: int = 0
    error: str | None = None
    prompt_version: str = "research-v2"

    @property
    def brief_dict(self) -> dict[str, Any]:
        return self.brief.model_dump()
