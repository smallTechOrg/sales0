"""Plain frozen dataclasses returned by the scraper (architecture.md → Contracts)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Any

SessionReason = Literal["ok", "no_cookie", "redirected_login", "checkpoint", "captcha", "rate_limited", "error"]
ConnectReason = Literal["ok", "timeout", "window_closed", "cancelled", "error"]
BrowserChannel = Literal["chrome", "chromium"]
# Phase 1.2: what a card link points at (== ``scraper.urls.link_kind_of(url)``).
LinkKind = Literal["post", "profile", "search", "other"]
# Phase 1.2: which card-discovery strategy won on a results page.
CardStrategy = Literal["sdui", "urn", "listitem", "none"]
# Phase 1.4: why ``LinkedInSearcher.scan`` ended.
StopReason = Literal["cap", "no_growth", "max_scrolls", "no_results", "drift", "page_budget"]
URN_REF_PREFIX = "urn:li:"

# The optional post fields whose absence is counted into ``KeywordScrapeResult.field_misses``.
OPTIONAL_POST_FIELDS: tuple[str, ...] = ("author_name", "author_profile_url", "author_headline", "posted_relative")
# Phase 1.2: ``field_misses["text"]`` counts result containers with a ref but no readable text.
TEXT_FIELD = "text"
# Audit #7: ``field_misses["truncated"]`` counts posts whose text still had an unexpanded "see more".
TRUNCATED_FIELD = "truncated"


@dataclass(frozen=True)
class ScrapedPost:
    # Phase 1.2 (was ``urn``): "urn:li:activity|ugcPost|share:N" when known, else "ck:<KEY>".
    source_ref: str
    url: str  # Phase 1.2: the card link — post permalink | author profile URL | search URL
    author_name: str | None
    author_profile_url: str | None
    author_headline: str | None
    posted_relative: str | None
    posted_date: date | None
    text: str
    link_kind: LinkKind = "post"  # Phase 1.2: == scraper.urls.link_kind_of(url)
    search_url: str | None = None  # Phase 1.2: the keyword's search URL (provenance)
    truncated: bool = False  # audit #7: the card still had an unexpanded "see more" / ellipsis marker
    # p11 maximum raw capture: innerText, reaction/comment/repost counts, links, media counts, capped HTML ...
    raw_card: dict[str, Any] | None = None

    @property
    def urn(self) -> str | None:
        """The post URN when the source ref is one, else None (a ``ck:`` ref)."""
        return self.source_ref if self.source_ref.startswith(URN_REF_PREFIX) else None


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
    #   Phase 1.2 adds key "text" (containers with a ref but no readable text).
    field_misses: dict[str, int] = field(default_factory=dict)
    strategy: CardStrategy = "none"  # Phase 1.2: which card strategy won on this page
    permalink_attempts: int = 0  # Phase 1.2: "Copy link to post" attempts
    permalinks_found: int = 0  # Phase 1.2: attempts that yielded a post URL
    passes: int = 1  # Phase 1.4: search passes run (one per date filter)
    expanded: int = 0  # Phase 1.4: "see more" clicks made
    stop_reason: StopReason = "cap"  # Phase 1.4: why the scan ended
    page_loads: int = 0  # audit #8: search page loads this scan spent (daily page-load budget)


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


@dataclass(frozen=True)
class LinkCaptureResult:
    """Phase 1.4: what ``LinkedInSearcher.capture_links`` achieved."""

    links: dict[str, str] = field(default_factory=dict)  # source_ref -> canonical permalink URL (hits only)
    attempts: int = 0  # "Copy link to post" attempts (cards whose menu was opened)
    found: int = 0  # attempts that yielded a post link
