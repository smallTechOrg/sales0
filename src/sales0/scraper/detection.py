"""Account-safety detection: logout/authwall, checkpoint, captcha, rate limit.

``classify`` is pure (unit-tested); ``detect_safety_stop`` reads the signals off a live
Playwright page. Callers raise ``AccountSafetyStop`` on any non-None kind — never retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from sales0.scraper import selectors
from sales0.scraper.errors import SafetyKind

if TYPE_CHECKING:
    from playwright.sync_api import Page, Response

RATE_LIMIT_STATUSES = frozenset({429, 999})
_LOGIN_SEGMENTS = frozenset({"authwall", "login", "login-submit", "signup", "uas"})
_CHECKPOINT_SEGMENTS = frozenset({"checkpoint"})


@dataclass(frozen=True)
class PageSignals:
    url: str
    status: int | None
    has_captcha: bool
    has_logged_in_marker: bool


def _segments(url: str) -> set[str]:
    return {part.lower() for part in urlsplit(url).path.split("/") if part}


def classify(signals: PageSignals) -> SafetyKind | None:
    """Return the safety-stop kind for a page, or None when it is a normal page.

    Only the URL *path* is inspected, so a search keyword like "login" never trips it.
    A missing logged-in marker on its own is not classified here; callers decide.
    """
    if signals.status in RATE_LIMIT_STATUSES:
        return "rate_limited"
    if signals.has_captcha:
        return "captcha"
    segments = _segments(signals.url)
    if segments & _LOGIN_SEGMENTS:
        return "logout"
    if segments & _CHECKPOINT_SEGMENTS:
        return "checkpoint"
    return None


def same_site(url: str, base: str) -> bool:
    """True when ``url`` is served by the site ``base`` points at.

    Live: any ``linkedin.com`` host (``www.``, ``static.``, the bare domain...), so a 429/999
    on a background ``/voyager/api/...`` call counts. Fixture: the exact fixture host:port.
    """
    target, home = urlsplit(url), urlsplit(base)
    if target.scheme not in ("http", "https"):
        return False
    host, home_host = (target.hostname or "").lower(), (home.hostname or "").lower()
    if home_host == "linkedin.com" or home_host.endswith(".linkedin.com"):
        return host == "linkedin.com" or host.endswith(".linkedin.com")
    return target.netloc.lower() == home.netloc.lower()


def is_rate_limit_response(url: str, status: int, base: str) -> bool:
    """A response (document, XHR or fetch) that means LinkedIn is throttling this session."""
    return status in RATE_LIMIT_STATUSES and same_site(url, base)


def read_signals(page: Page, response: Response | None) -> PageSignals:
    return PageSignals(
        url=page.url,
        status=response.status if response is not None else None,
        has_captcha=page.locator(selectors.CAPTCHA_MARKER).count() > 0,
        has_logged_in_marker=page.locator(selectors.LOGGED_IN_MARKER).count() > 0,
    )


def detect_safety_stop(page: Page, response: Response | None) -> SafetyKind | None:
    return classify(read_signals(page, response))
