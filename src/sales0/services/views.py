"""Typed, frozen view models returned by services to routes (architecture.md -> Contracts).

Routes never touch ORM objects; they receive these. Fields beyond the contract are
additive only (marked "additive" below) so consumers coded against the contract keep
working.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

SessionStatus = Literal["not_connected", "connected", "needs_attention", "busy"]
BusyAction = Literal["connect", "check", "run"]
InboxTab = Literal["review", "accepted", "discarded"]


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class KeywordView(_View):
    id: int
    text: str
    active: bool
    last_run_at: datetime | None
    created_at: datetime


class OfferingView(_View):
    id: int
    name: str
    group_id: int
    group_name: str
    description: str
    active: bool
    sort_order: int
    enquiry_count: int


class OfferingInput(_View):
    """Form input for create/update. Lengths (name 1..80, group 1..60, description 0..500)
    are enforced by the service, which raises ``DomainError(offering_invalid)`` — so a route
    can build this from any form values without catching a ValidationError."""

    name: str = ""
    group_name: str = ""
    description: str = ""

    @field_validator("name", "group_name", "description", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        if value is None:
            return ""
        return value.strip() if isinstance(value, str) else value


class SessionStatusView(_View):
    status: SessionStatus
    busy_action: BusyAction | None
    last_checked_at: datetime | None
    last_successful_run_at: datetime | None
    last_error: str | None
    mode: Literal["live", "fixture"]
    connect_elapsed_s: int | None
    # additive: the stored status underneath "busy" (never "busy" itself)
    stored_status: Literal["not_connected", "connected", "needs_attention"] = "not_connected"
    last_connected_at: datetime | None = None


class Eligibility(_View):
    can_run: bool
    reason: str | None
    runs_today: int
    runs_cap: int
    posts_today: int
    posts_cap: int
    # additive: the DomainError code matching ``reason`` (api.md), None when can_run
    reason_code: str | None = None


class RunKeywordView(_View):
    keyword_text: str
    status: str
    posts_seen: int
    posts_new: int
    posts_dupe: int
    errors: int
    duration_ms: int | None
    error_message: str | None


class RunView(_View):
    id: int
    status: Literal["queued", "running", "succeeded", "aborted", "failed"]
    trigger: str
    mode: str
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    keywords_planned: int
    keywords_done: int
    current_keyword: str | None
    current_step: str | None
    posts_seen: int
    posts_new: int
    posts_dupe: int
    enquiries_triaged: int
    triage_failed: int
    errors_count: int
    abort_reason: str | None
    error_message: str | None
    cost_usd_micros: int

    @property
    def is_active(self) -> bool:
        return self.status in ("queued", "running")


class RunDetailView(_View):
    run: RunView
    keywords: list[RunKeywordView]


class EnquiryCardView(_View):
    id: int
    review_status: Literal["pending_review", "accepted", "discarded"]
    stage: str | None
    triage_status: Literal["pending", "done", "failed"]
    triage_error: str | None
    requirement_summary: str | None
    ai_verdict: Literal["genuine", "discard"] | None
    ai_reason: str | None
    ai_poster_type: str | None
    priority: str | None
    offering_id: int | None
    offering_name: str | None
    company: str | None
    contact_person: str | None
    email: str | None
    phone: str | None
    city: str | None
    mode: str | None
    dates_mentioned: str | None
    topic: str | None
    author_name: str | None
    author_headline: str | None
    source_url: str | None
    raw_text: str
    date_posted: date | None
    date_added: datetime
    keywords: list[str]
    discard_reason: str | None
    is_override: bool
    decided_at: datetime | None
    cost_usd_micros: int
    tokens_total: int
    # additive
    source_ref: str | None = None  # first source URN (ui.md: data-urn)
    ai_priority: str | None = None
    ai_offering_id: int | None = None


class InboxPage(_View):
    tab: InboxTab
    q: str | None
    items: list[EnquiryCardView]
    total: int
    page: int
    page_size: int
    counts: dict[str, int]  # keys: review, accepted, discarded

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page * self.page_size < self.total


class CostSummary(_View):
    today_usd_micros: int
    month_usd_micros: int
    today_calls: int
    month_calls: int


__all__ = [
    "BusyAction",
    "CostSummary",
    "Eligibility",
    "EnquiryCardView",
    "InboxPage",
    "InboxTab",
    "KeywordView",
    "OfferingInput",
    "OfferingView",
    "RunDetailView",
    "RunKeywordView",
    "RunView",
    "SessionStatus",
    "SessionStatusView",
]
