"""Typed inputs and outputs of the triage agent (architecture.md -> Contracts -> Agent).

``TriageResult`` doubles as the ``record_triage`` tool input schema. Its Pydantic
validation is deliberately lenient on lengths and casing (the guards in ``guards.py``
normalise those), and strict on the enum values the inbox depends on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sales0.config import Settings
from sales0.llm.client import LLMClient, LLMResponse

Verdict = Literal["genuine", "discard"]
PosterType = Literal[
    "buyer_company",
    "training_agency",
    "individual_trainer_seeker",
    "job_seeker_or_self_promo",
    "other",
]
Priority = Literal["high", "medium", "low"]
Mode = Literal["online", "offline", "hybrid", "unknown"]


class OfferingRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    group: str
    description: str


class DecisionExample(BaseModel):
    """One past team decision, used as a few-shot example (the learning loop)."""

    model_config = ConfigDict(frozen=True)

    enquiry_id: int
    text: str
    decision: Literal["accept", "discard"]
    ai_verdict: Verdict | None
    is_override: bool
    reason: str | None
    decided_at: datetime


class TriageInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    enquiry_id: int
    text: str
    author_name: str | None = None
    author_headline: str | None = None
    source_url: str | None = None
    date_posted: date | None = None


def _lower_strip(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


class TriageResult(BaseModel):
    """The ``record_triage`` tool input. Field descriptions are shown to the model."""

    model_config = ConfigDict(extra="ignore")

    verdict: Verdict = Field(
        description="genuine = a company/team wants to BUY training, a workshop, consulting/build or a product; "
        "discard = agency/marketplace recruiting freelance trainers, individual-trainer seeker at hourly/per-day "
        "rates, trainer self-promotion/open to work, far outside the offerings, or not a requirement."
    )
    reason: str = Field(
        description="ONE line, max 160 characters, starting with the decisive fact.",
    )
    poster_type: PosterType = Field(description="Who posted it.")
    requirement_summary: str = Field(
        default="",
        description='Format "Company - Topic - City/Mode", max 140 characters, one line.',
    )
    priority: Priority = Field(
        description="high = genuine, clear corporate need, contact or route available, near-term; "
        "medium = genuine but vague, distant or no contact route; low = marginal or discard."
    )
    offering_id: int | None = Field(
        default=None, description="The id of the best-matching offering from the catalogue, or null."
    )
    company: str | None = Field(default=None, description="Exactly as written in the post, or null.")
    contact_person: str | None = Field(default=None, description="Exactly as written in the post, or null.")
    email: str | None = Field(
        default=None, description="Copied character-for-character from the post, or null. Never guessed."
    )
    phone: str | None = Field(
        default=None, description="Copied character-for-character from the post, or null. Never guessed."
    )
    city: str | None = Field(default=None, description="Exactly as written in the post, or null.")
    mode: Mode = Field(default="unknown", description="Delivery mode of the requirement.")
    dates_mentioned: str | None = Field(default=None, description="Dates/timing as free text, or null.")
    topic: str | None = Field(default=None, description="Short topic, or null.")
    language: str = Field(default="und", description="ISO 639-1 code of the post's main language (en, hi, ...).")

    @field_validator("verdict", "poster_type", "priority", "mode", mode="before")
    @classmethod
    def _normalise_enum(cls, value: Any) -> Any:
        return _lower_strip(value)

    @field_validator("offering_id", mode="before")
    @classmethod
    def _blank_offering_is_null(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip().lower() in {"", "null", "none"}:
            return None
        return value


def triage_tool_schema() -> dict[str, Any]:
    """JSON Schema of the ``record_triage`` input: every property listed as required.

    Optional values are expressed as nullable, so the model always answers every field
    explicitly (null instead of omission) while validation stays lenient.
    """
    schema = TriageResult.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
        prop.pop("default", None)
    schema["required"] = list(schema.get("properties", {}))
    schema["additionalProperties"] = False
    return schema


class TriageOutcome(BaseModel):
    status: Literal["done", "failed"]
    result: TriageResult | None
    error: str | None
    llm_responses: list[LLMResponse]
    fewshot_count: int
    prompt_version: str


@dataclass(frozen=True)
class TriageDeps:
    llm: LLMClient
    offerings: list[OfferingRef]
    examples: list[DecisionExample]
    settings: Settings
