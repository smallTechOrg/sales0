"""Allowed values of every enum-like column (stored as lowercase strings; see data.md).

The tuples feed both the CHECK constraints (models + migration 0002) and the ``Literal``
types other layers validate against.
"""

from __future__ import annotations

from typing import Literal

LinkedInSessionStatus = Literal["not_connected", "connected", "needs_attention"]
LINKEDIN_SESSION_STATUSES: tuple[str, ...] = ("not_connected", "connected", "needs_attention")

RunTrigger = Literal["manual"]
RUN_TRIGGERS: tuple[str, ...] = ("manual",)

RunMode = Literal["live", "fixture"]
RUN_MODES: tuple[str, ...] = ("live", "fixture")

RunStatus = Literal["queued", "running", "succeeded", "aborted", "failed"]
RUN_STATUSES: tuple[str, ...] = ("queued", "running", "succeeded", "aborted", "failed")

AbortReason = Literal["logout", "checkpoint", "captcha", "rate_limited", "daily_post_cap"]
ABORT_REASONS: tuple[str, ...] = ("logout", "checkpoint", "captcha", "rate_limited", "daily_post_cap")

RunKeywordStatus = Literal["pending", "running", "done", "warning", "error", "skipped"]
RUN_KEYWORD_STATUSES: tuple[str, ...] = ("pending", "running", "done", "warning", "error", "skipped")

EnquirySourceKind = Literal["linkedin_post"]
ENQUIRY_SOURCES: tuple[str, ...] = ("linkedin_post",)

ReviewStatus = Literal["pending_review", "accepted", "discarded"]
REVIEW_STATUSES: tuple[str, ...] = ("pending_review", "accepted", "discarded")

EnquiryStage = Literal["prospect_new"]
ENQUIRY_STAGES: tuple[str, ...] = ("prospect_new",)

TriageStatus = Literal["pending", "done", "failed"]
TRIAGE_STATUSES: tuple[str, ...] = ("pending", "done", "failed")

Verdict = Literal["genuine", "discard"]
VERDICTS: tuple[str, ...] = ("genuine", "discard")

Priority = Literal["high", "medium", "low"]
PRIORITIES: tuple[str, ...] = ("high", "medium", "low")

EngagementMode = Literal["online", "offline", "hybrid", "unknown"]
ENGAGEMENT_MODES: tuple[str, ...] = ("online", "offline", "hybrid", "unknown")

Decision = Literal["accept", "discard"]
DECISIONS: tuple[str, ...] = ("accept", "discard")

LlmFeature = Literal["ping", "triage"]
LLM_FEATURES: tuple[str, ...] = ("ping", "triage", "research", "drafting", "suggest")

LlmCallStatus = Literal["ok", "error"]
LLM_CALL_STATUSES: tuple[str, ...] = ("ok", "error")

HistoryEntityType = Literal["enquiry", "scrape_run", "linkedin_session"]
HISTORY_ENTITY_TYPES: tuple[str, ...] = ("enquiry", "scrape_run", "linkedin_session")
