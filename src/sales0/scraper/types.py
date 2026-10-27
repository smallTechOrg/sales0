"""Plain frozen dataclasses returned by the scraper (architecture.md → Contracts)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

SessionReason = Literal["ok", "no_cookie", "redirected_login", "checkpoint", "captcha", "rate_limited", "error"]
ConnectReason = Literal["ok", "timeout", "window_closed", "cancelled", "error"]
BrowserChannel = Literal["chrome", "chromium"]

# The optional post fields whose absence is counted into ``KeywordScrapeResult.field_misses``.
OPTIONAL_POST_FIELDS: tuple[str, ...] = ("author_name", "author_profile_url", "author_headline", "posted_relative")


@dataclass(frozen=True)
class ScrapedPost:
    urn: str
    url: str
    author_name: str | None
    author_profile_url: str | None
    author_headline: str | None
    posted_relative: str | None
    posted_date: date | None
    text: str


@dataclass(frozen=True)
class KeywordScrapeResult:
    keyword: str
    posts: list[ScrapedPost]
    scrolls: int
    parse_errors: int
    drift_warning: bool
    cards_found: int = 0  # Phase 1.1: card elements discovered (before parsing)
    no_results: bool = False  # Phase 1.1: the "no results" marker was present
    # Phase 1.1: optional field name → number of parsed posts missing it (only non-zero entries).
    field_misses: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionCheckResult:
    logged_in: bool
    reason: SessionReason
    url: str
    soft_hints: tuple[str, ...] = ()  # Phase 1.1: matched soft DOM hints (logged only, never a verdict)


@dataclass(frozen=True)
class ConnectResult:
    connected: bool
    reason: ConnectReason
    detail: str | None
    browser: BrowserChannel | None = None  # Phase 1.1
