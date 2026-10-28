"""LinkedIn URLs. Fixture mode points at the app's own ``/__fixtures/linkedin`` routes, whose
paths have the same shape as LinkedIn's.

Phase 1.2: the PURE URL helpers shared by the scraper and the services live here, in one
place (architecture.md → "SDUI field extraction" / "Permalink capture"): ``urn_from_href``,
``canonical_post_url``, ``link_kind_of``, ``profile_slug``, plus the site rules
``same_site`` / ``linkedin_path`` (re-exported by ``detection``).
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit

from sales0.config import Settings
from sales0.scraper import selectors as S
from sales0.scraper.types import LinkKind

LIVE_BASE = "https://www.linkedin.com"
FIXTURE_PREFIX = "/__fixtures/linkedin"
DATE_FILTERS = frozenset({"past-24h", "past-week", "past-month"})
# Phase 1.4: LinkedIn's sortBy facet values (latest = by date posted).
SORT_VALUES: dict[str, str] = {"latest": '"date_posted"', "relevance": '"relevance"'}

FEED_UPDATE_HREF = re.compile(S.FEED_UPDATE_HREF_RE)
POSTS_SLUG_HREF = re.compile(S.POSTS_SLUG_HREF_RE)
_PROFILE_PATH = re.compile(r"^/(in|company)/([^/?#]+)")


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


def search_url(base: str, keyword: str, date_filter: str, sort: Literal["latest", "relevance"] = "latest") -> str:
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("keyword must not be blank")
    if date_filter not in DATE_FILTERS:
        raise ValueError(f"Unknown date filter {date_filter!r}; use one of {sorted(DATE_FILTERS)}")
    if sort not in SORT_VALUES:
        raise ValueError(f"Unknown sort {sort!r}; use one of {sorted(SORT_VALUES)}")
    query = urlencode(
        {
            "keywords": keyword,
            "datePosted": f'"{date_filter}"',
            "sortBy": SORT_VALUES[sort],
            "origin": "FACETED_SEARCH",
        },
        quote_via=quote,
    )
    return f"{base}/search/results/content/?{query}"


# --------------------------------------------------------------------------- site rules
def strip_fixture_prefix(path: str) -> str:
    """``/__fixtures/linkedin/feed/`` → ``/feed/``; any other path is returned unchanged."""
    if path == FIXTURE_PREFIX or path.startswith(FIXTURE_PREFIX + "/"):
        return path[len(FIXTURE_PREFIX) :] or "/"
    return path


def linkedin_path(url: str) -> str | None:
    """The LinkedIn-shaped path of ``url`` (fixture prefix removed), or None for non-web URLs."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    return strip_fixture_prefix(parts.path or "/")


def same_site(url: str, base: str) -> bool:
    """True when ``url`` is served by the site ``base`` points at.

    Live: any ``linkedin.com`` host (``www.``, the bare domain...), so a 429/999 on a
    background ``/voyager/api/...`` call counts. Fixture: the exact fixture host:port.
    """
    try:
        target, home = urlsplit(url), urlsplit(base)
        host, home_host = (target.hostname or "").lower(), (home.hostname or "").lower()
    except ValueError:
        return False
    if target.scheme not in ("http", "https"):
        return False
    if home_host == "linkedin.com" or home_host.endswith(".linkedin.com"):
        return host == "linkedin.com" or host.endswith(".linkedin.com")
    return target.netloc.lower() == home.netloc.lower()


# --------------------------------------------------------------------------- post links
def urn_from_href(href: object) -> str | None:
    """``/feed/update/urn:li:activity:N/`` or ``/posts/<slug>-activity-N-xxxx/`` → the post URN."""
    if not isinstance(href, str) or not href:
        return None
    href = unquote(href)
    match = FEED_UPDATE_HREF.search(href)
    if match:
        return match.group(1)
    match = POSTS_SLUG_HREF.search(href)
    if match:
        return f"urn:li:{match.group(1)}:{match.group(2)}"
    return None


def _path_of(url: str) -> str | None:
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    return strip_fixture_prefix(path or "/")


def _is_post_path(path: str) -> bool:
    return "/feed/update/" in path or path.startswith("/posts/")


def canonical_post_url(copied: object, base: str) -> tuple[str | None, str | None]:
    """A "Copy link to post" result → ``(urn | None, url | None)`` (pure).

    Non-same-site (or not a URL) → ``(None, None)``. A URN in the link → ``(urn,
    permalink(base, urn))``. Otherwise a same-site ``/posts/…`` or ``/feed/update/…`` path →
    ``(None, url without query/fragment)``, so tracking query strings (``utm_*``, ``rcm``)
    are never stored. Anything else → ``(None, None)``.
    """
    if not isinstance(copied, str) or not copied.strip():
        return None, None
    url = copied.strip()
    if url.startswith("/") and not url.startswith("//"):
        url = base.rstrip("/") + url  # a site-relative link: resolve against the base
    if not same_site(url, base):
        return None, None
    urn = urn_from_href(url)
    if urn is not None:
        return urn, permalink(base, urn)
    parts = urlsplit(url)
    if _is_post_path(strip_fixture_prefix(parts.path or "/")):
        return None, urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return None, None


def link_kind_of(url: str | None) -> LinkKind | None:
    """What a card link points at: ``post`` | ``profile`` | ``search`` | ``other``; None for empty."""
    if not isinstance(url, str) or not url.strip():
        return None
    path = _path_of(url.strip())
    if path is None:
        return "other"
    if _is_post_path(path):
        return "post"
    if path.startswith(("/in/", "/company/")):
        return "profile"
    if path.startswith("/search/"):
        return "search"
    return "other"


def profile_slug(url: str | None) -> str | None:
    """``…/in/Priya-Menon%2D1/?x`` → ``"in/priya-menon-1"``; ``…/company/acme/`` → ``"company/acme"``."""
    if not isinstance(url, str) or not url.strip():
        return None
    path = _path_of(url.strip())
    if path is None:
        return None
    match = _PROFILE_PATH.match(path)
    if not match:
        return None
    slug = unquote(match.group(2)).strip().lower()
    return f"{match.group(1)}/{slug}" if slug else None
