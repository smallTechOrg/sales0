"""Suggestions agent: structured output (validated) and the batch result."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, field_validator

PROMPT_VERSION = "suggest-v1"  # bump on any prompt or schema edit
TOOL_NAME = "record_suggestions"
PHRASE_MIN, PHRASE_MAX = 2, 100
RATIONALE_MAX = 300


def _squash(value: Any) -> Any:
    return " ".join(value.split()) if isinstance(value, str) else value


class NewKeyword(BaseModel):
    phrase: str = Field(min_length=PHRASE_MIN, max_length=PHRASE_MAX)
    rationale: str = Field(default="", max_length=RATIONALE_MAX)
    evidence: list[int] = Field(default_factory=list, max_length=25)

    @field_validator("phrase", mode="before")
    @classmethod
    def _phrase(cls, v: Any) -> Any:
        v = _squash(v)
        return v.strip(" \"'") if isinstance(v, str) else v

    @field_validator("rationale", mode="before")
    @classmethod
    def _rationale(cls, v: Any) -> Any:
        v = _squash(v)
        return v[:RATIONALE_MAX] if isinstance(v, str) else v  # clip, do not reject


class PauseKeyword(BaseModel):
    keyword_id: int
    rationale: str = Field(default="", max_length=RATIONALE_MAX)

    @field_validator("rationale", mode="before")
    @classmethod
    def _rationale(cls, v: Any) -> Any:
        v = _squash(v)
        return v[:RATIONALE_MAX] if isinstance(v, str) else v


class SuggestOutput(BaseModel):
    new_keywords: list[NewKeyword] = Field(default_factory=list, max_length=20)
    pause_keywords: list[PauseKeyword] = Field(default_factory=list, max_length=20)


def tool_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "new_keywords": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "phrase": {
                            "type": "string",
                            "description": "a LinkedIn search phrase, 2-100 characters",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "why, at most 300 characters",
                        },
                        "evidence": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "numbers of the enquiries it is based on",
                        },
                    },
                    "required": ["phrase", "rationale", "evidence"],
                },
            },
            "pause_keywords": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "keyword_id": {"type": "integer"},
                        "rationale": {
                            "type": "string",
                            "description": "at most 300 characters",
                        },
                    },
                    "required": ["keyword_id", "rationale"],
                },
            },
        },
        "required": ["new_keywords", "pause_keywords"],
    }


@dataclass
class SuggestionBatch:
    """Result of one run: ids of the rows created plus why anything was dropped."""

    created_ids: list[int] = field(default_factory=list)
    new_count: int = 0
    pause_count: int = 0
    dropped: list[str] = field(default_factory=list)
    llm_called: bool = False
    llm_calls: int = 0
    cost_usd_micros: int = 0
    message: str = ""

    @property
    def created(self) -> int:
        return len(self.created_ids)
