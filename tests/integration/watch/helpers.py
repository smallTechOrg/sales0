"""Builders for the prospect-watch tests: tracked profiles + a scripted searcher with ``scan_profile``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sales0.clock import utcnow
from sales0.db.engine import session_scope
from sales0.db.models import LinkedinProfile
from sales0.scraper.types import KeywordScrapeResult, ScrapedPost
from tests.integration.services.helpers import LIVE, post, sdui_post  # noqa: F401


def add_profile(slug: str, *, tracked: bool | None = True, name: str | None = None, checked_days_ago: float | None = None,
                paused: bool = False) -> int:
    now = utcnow()
    with session_scope() as db:
        row = LinkedinProfile(
            slug=slug, kind="company" if slug.startswith("company/") else "person", profile_url=f"{LIVE}/{slug}/",
            display_name=name or slug.split("/")[1].title(), headline="Head of L&D", first_seen_at=now, last_seen_at=now,
            tracked=tracked,
            last_checked_at=(now - timedelta(days=checked_days_ago)) if checked_days_ago is not None else None,
        )
        if paused:
            row.watch_disabled_until = now + timedelta(days=3650)
        db.add(row)
        db.flush()
        return row.id


def profile_post(slug: str, key: str, text: str, *, author_url: str | None = "") -> ScrapedPost:
    """An SDUI card on ``slug``'s activity page (author defaults to the profile itself)."""
    base = sdui_post(key, text, slug=slug)
    if author_url == "":
        return base
    from dataclasses import replace

    return replace(base, author_profile_url=author_url)


@dataclass
class WatchSearcher:
    """``slug -> posts`` (or an exception to raise from ``scan_profile``)."""

    results: dict[str, list[ScrapedPost] | Exception]
    enter_error: Exception | None = None
    visited: list[str] = field(default_factory=list)
    max_posts_seen: list[int] = field(default_factory=list)
    drift: set[str] = field(default_factory=set)
    on_scan: Any = None
    closed: bool = False

    def __call__(self, _settings: Any) -> WatchSearcher:
        return self

    def __enter__(self) -> WatchSearcher:
        if self.enter_error is not None:
            raise self.enter_error
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True

    def scan_profile(self, slug: str, max_posts: int) -> KeywordScrapeResult:
        self.visited.append(slug)
        self.max_posts_seen.append(max_posts)
        if self.on_scan is not None:
            self.on_scan(slug)
        item = self.results.get(slug, [])
        if isinstance(item, Exception):
            raise item
        return KeywordScrapeResult(
            keyword=f"watch:{slug}", posts=list(item)[:max_posts], scrolls=1, parse_errors=0,
            drift_warning=slug in self.drift, cards_found=len(item), no_results=not item,
        )
