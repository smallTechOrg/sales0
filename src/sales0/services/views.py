"""Typed, frozen view models returned by services to routes (architecture.md -> Contracts).

Routes never touch ORM objects; they receive these. Fields beyond the contract are
additive only (marked "additive" below) so consumers coded against the contract keep
working.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from sales0.scraper.types import LinkKind

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
    # Phase 1.4
    group_name: str | None = None
    offering_id: int | None = None
    offering_active: bool = True  # False = its offering is off, so runs skip it
    suggested: bool = False
    scanned_total: int = 0
    relevant_total: int = 0
    last_scanned: int = 0
    last_relevant: int = 0


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
    # Phase 1.1: the browser sales0 uses (scraper.browser.resolve_browser), e.g. label
    # "Google Chrome 156" / "Built-in browser (Chromium)"; note = the auto-fallback note, or
    # the BrowserUnavailable message (then label "Google Chrome (not installed)").
    browser_label: str = "Google Chrome"
    browser_note: str | None = None
    # additive: the resolved channel; None when the browser is unavailable
    browser_channel: Literal["chrome", "chromium"] | None = None
    # Phase 1.1: services.runs.safety_cooldown_until (UTC); None = no cool-down in effect
    cooldown_until: datetime | None = None


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
    # Phase 1.4
    posts_relevant: int = 0
    posts_skipped_ai: int = 0
    permalinks_found: int = 0
    scrolls: int = 0
    cost_usd_micros: int = 0


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
    # Phase 1.1: number of this run's keyword rows with status "warning" (computed on read)
    layout_warnings: int = 0
    # Phase 1.4
    posts_relevant: int = 0
    posts_skipped_ai: int = 0
    permalinks_found: int = 0
    # q14: genuine posts auto-discarded as repeats from an existing prospect (beyond the first in the run)
    posts_repeat_skipped: int = 0
    # relevant / new x 100; None when no post was new
    relevance_pct: float | None = None

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
    source_ref: str | None = None  # first source ref: a URN or ck:<KEY> (ui.md: data-urn)
    ai_priority: str | None = None
    ai_offering_id: int | None = None
    # Phase 1.2: scraper.urls.link_kind_of(source_url), derived on read (never stored);
    # None when source_url is null. Drives the card's link label (ui.md).
    link_kind: LinkKind | None = None
    # Phase 1.4
    posted_relative: str | None = None
    author_profile_url: str | None = None
    # Phase 1.5
    ai_opportunity_type: str | None = None
    # Prospect watch
    author_profile_id: int | None = None
    author_tracked: bool = False
    # q14 (inbox feedback A1): the existing pipeline entity this enquiry matched ("Already in pipeline: <name>")
    pipeline_name: str | None = None
    pipeline_href: str | None = None
    pipeline_match: str | None = None
    # q14: 'auto' = discarded by a system rule (repeat), not by the team
    discard_source: str | None = None
    # q16: the category the team has (default relevant_prospect) and the AI's suggestion; the competitor it was filed under
    category: str = "relevant_prospect"
    ai_category: str | None = None
    competitor_id: int | None = None
    competitor_name: str | None = None


class SkippedPostView(_View):
    """A post the AI skipped (or whose triage failed): one line, never a card."""

    id: int
    snippet: str
    reason: str | None
    poster_type: str | None
    author_name: str | None
    author_headline: str | None
    profile_url: str | None
    keyword_text: str
    post_url: str | None
    link_kind: LinkKind | None
    posted_relative: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    times_seen: int
    triage_status: Literal["pending", "done", "failed"]
    triage_error: str | None
    raw_text: str | None
    can_rescue: bool


class SkippedPage(_View):
    items: list[SkippedPostView]
    total: int
    page: int
    page_size: int
    state: Literal["discard", "failed"]
    q: str | None
    counts: dict[str, int]  # keys: discard, failed

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page * self.page_size < self.total


class ProfileView(_View):
    id: int
    slug: str
    kind: Literal["person", "company"]
    profile_url: str
    display_name: str | None
    headline: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    posts_seen: int
    relevant_count: int
    tracked: bool | None


class InboxPage(_View):
    tab: InboxTab
    q: str | None
    category: str | None = None
    items: list[EnquiryCardView]
    total: int
    page: int
    page_size: int
    counts: dict[str, int]  # keys: review, accepted, discarded, skipped, skipped_failed

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
    "ProfileView",
    "RunDetailView",
    "RunKeywordView",
    "RunView",
    "SessionStatus",
    "SessionStatusView",
    "SkippedPage",
    "SkippedPostView",
]
