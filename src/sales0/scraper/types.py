"""Plain frozen dataclasses returned by the scraper (architecture.md → Contracts)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

SessionReason = Literal["ok", "no_cookie", "redirected_login", "checkpoint", "captcha", "rate_limited", "error"]
ConnectReason = Literal["ok", "timeout", "window_closed", "cancelled", "error"]


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


@dataclass(frozen=True)
class SessionCheckResult:
    logged_in: bool
    reason: SessionReason
    url: str


@dataclass(frozen=True)
class ConnectResult:
    connected: bool
    reason: ConnectReason
    detail: str | None
