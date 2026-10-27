"""LinkedIn URLs. Fixture mode points at the app's own ``/__fixtures/linkedin`` routes, whose
paths have the same shape as LinkedIn's."""

from __future__ import annotations

from urllib.parse import quote, urlencode

from sales0.config import Settings

LIVE_BASE = "https://www.linkedin.com"
FIXTURE_PREFIX = "/__fixtures/linkedin"
DATE_FILTERS = frozenset({"past-24h", "past-week", "past-month"})


def base_url(settings: Settings) -> str:
    if settings.linkedin_mode == "fixture":
        return f"http://{settings.host}:{settings.port}{FIXTURE_PREFIX}"
    return LIVE_BASE


def feed_url(base: str) -> str:
    return f"{base}/feed/"


def login_url(base: str) -> str:
    return f"{base}/login"


def permalink(base: str, urn: str) -> str:
    return f"{base}/feed/update/{urn}/"


def search_url(base: str, keyword: str, date_filter: str) -> str:
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("keyword must not be blank")
    if date_filter not in DATE_FILTERS:
        raise ValueError(f"Unknown date filter {date_filter!r}; use one of {sorted(DATE_FILTERS)}")
    query = urlencode(
        {
            "keywords": keyword,
            "datePosted": f'"{date_filter}"',
            "sortBy": '"date_posted"',
            "origin": "FACETED_SEARCH",
        },
        quote_via=quote,
    )
    return f"{base}/search/results/content/?{query}"
