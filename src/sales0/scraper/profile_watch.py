"""Prospect watch: read one tracked profile's own posts from its activity page.

People: ``/in/<slug>/recent-activity/all/``; companies: ``/company/<slug>/posts/``. The page is
parsed with the SAME card tiers as search results (sdui -> urn -> listitem), so ``scan_profile`` is
the search scan minus the date/sort passes, with the caller's profile-visit safety: it goes through
``LinkedInSearcher._scan_pass`` (checkpoint before/after the navigation and every scroll, pacer delay
before every page action, latch listeners) and never opens a menu or clicks anything except "see more".

``own_posts`` keeps the posts authored by the profile (or with no readable author, which on an own-
activity page means the profile): the activity feed also lists posts the profile merely reacted to or
commented on, which are not the profile's requirements.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from sales0.clock import local_today
from sales0.logging import get_logger
from sales0.scraper.errors import ScrapeError
from sales0.scraper.parsing import drift_detected, parse_cards
from sales0.scraper.types import KeywordScrapeResult, ScrapedPost
from sales0.scraper.urls import profile_activity_url, profile_slug

log = get_logger("sales0.scraper.profile_watch")


def scan_profile(searcher: Any, slug: str, max_posts: int) -> KeywordScrapeResult:
    """Visit the activity page of ``slug`` (``in/x`` | ``company/x``) with an OPEN ``LinkedInSearcher``
    and return up to ``max_posts`` parsed posts. Raises ``AccountSafetyStop`` / ``ScrapeError``."""
    page = getattr(searcher, "_page", None)
    if page is None or not searcher.is_open:
        raise ScrapeError("LinkedInSearcher is not open (use it as a context manager)")
    settings = searcher._settings
    base = searcher._base
    url = profile_activity_url(base, slug)
    cap = max(1, min(max_posts, settings.watch_max_posts_per_profile))
    started = time.monotonic()
    searcher._last_keyword = f"watch:{slug}"
    outcome = searcher._scan_pass(page, f"watch:{slug}", "", url, cap, 0, f" for {slug}")
    today = local_today(settings.tz)
    parsed = parse_cards(outcome.raws, base, today, cap, search_url=url)
    searcher._remember_cards([outcome.raws])
    drift = drift_detected(len(parsed.posts), max(outcome.cards_found, parsed.cards_found), outcome.no_results)
    if drift:
        log.warning("watch_layout_drift", slug=slug, cards_found=outcome.cards_found, parse_errors=parsed.parse_errors)
    log.debug(
        "watch_profile_scanned", slug=slug, strategy=parsed.strategy, cards=outcome.cards_found,
        posts=len(parsed.posts), scrolls=outcome.scrolls, duration_ms=int((time.monotonic() - started) * 1000),
    )
    return KeywordScrapeResult(
        keyword=f"watch:{slug}",
        posts=list(parsed.posts),
        scrolls=outcome.scrolls,
        parse_errors=parsed.parse_errors,
        drift_warning=drift and not parsed.posts,
        cards_found=max(outcome.cards_found, parsed.cards_found),
        no_results=outcome.no_results,
        field_misses=dict(parsed.field_misses),
        strategy=parsed.strategy,
        passes=1,
        expanded=outcome.expanded,
        stop_reason="drift" if drift else outcome.stop,
    )


def own_posts(posts: list[ScrapedPost], slug: str, profile_url: str) -> tuple[list[ScrapedPost], int]:
    """``(posts authored by the profile, number of others' posts skipped)``. A post with no readable
    author link is the profile's own and gets the tracked profile's URL, so it is attributed to it."""
    mine: list[ScrapedPost] = []
    foreign = 0
    for post in posts:
        author = profile_slug(post.author_profile_url)
        if author is None:
            mine.append(replace(post, author_profile_url=profile_url))
        elif author == slug:
            mine.append(post)
        else:
            foreign += 1
    return mine, foreign


__all__ = ["own_posts", "scan_profile"]
