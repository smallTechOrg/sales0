"""Session check: is the persistent profile logged in to LinkedIn? Never logs in."""

from __future__ import annotations

from playwright.sync_api import BrowserContext
from playwright.sync_api import Error as PlaywrightError

from sales0.config import Settings
from sales0.logging import get_logger
from sales0.scraper import selectors
from sales0.scraper.browser import first_page, open_context, profile_dir, wait_timeout_ms
from sales0.scraper.detection import classify, read_signals
from sales0.scraper.errors import SafetyKind, ScrapeError
from sales0.scraper.types import SessionCheckResult, SessionReason
from sales0.scraper.urls import base_url, feed_url

log = get_logger("sales0.scraper.session")

_KIND_TO_REASON: dict[SafetyKind, SessionReason] = {
    "logout": "redirected_login",
    "checkpoint": "checkpoint",
    "captcha": "captcha",
    "rate_limited": "rate_limited",
}
# How a failed check becomes a run-aborting stop (used by LinkedInSearcher.__enter__).
REASON_TO_KIND: dict[SessionReason, SafetyKind] = {
    "no_cookie": "logout",
    "redirected_login": "logout",
    "checkpoint": "checkpoint",
    "captcha": "captcha",
    "rate_limited": "rate_limited",
}


def has_session_cookie(context: BrowserContext, base: str) -> bool:
    return any(c.get("name") == selectors.SESSION_COOKIE and c.get("value") for c in context.cookies(base))


def verify_logged_in(context: BrowserContext, base: str, timeout_ms: int) -> SessionCheckResult:
    """Open ``{base}/feed/`` in ``context`` and classify the result. No retries, no login."""
    url = feed_url(base)
    if not has_session_cookie(context, base):
        return SessionCheckResult(logged_in=False, reason="no_cookie", url=url)
    page = first_page(context)
    try:
        response = page.goto(url, wait_until="domcontentloaded")
    except PlaywrightError as exc:
        log.warning("scraper_session_error", url=url, error=str(exc))
        return SessionCheckResult(logged_in=False, reason="error", url=url)
    kind = classify(read_signals(page, response))
    if kind is None:
        try:
            page.wait_for_selector(selectors.LOGGED_IN_MARKER, state="attached", timeout=timeout_ms)
        except PlaywrightError:
            pass
        kind = classify(read_signals(page, None))
    if kind is not None:
        return SessionCheckResult(logged_in=False, reason=_KIND_TO_REASON[kind], url=page.url)
    if page.locator(selectors.LOGGED_IN_MARKER).count() == 0:
        return SessionCheckResult(logged_in=False, reason="redirected_login", url=page.url)
    return SessionCheckResult(logged_in=True, reason="ok", url=page.url)


def check_session(settings: Settings, *, headless: bool = True) -> SessionCheckResult:
    """Open the profile, check the feed, close. Fixture mode is always headless."""
    base = base_url(settings)
    headless = True if settings.linkedin_mode == "fixture" else headless
    try:
        with open_context(profile_dir(settings), headless) as context:
            result = verify_logged_in(context, base, wait_timeout_ms(settings))
    except (ScrapeError, PlaywrightError) as exc:
        log.warning("scraper_session_error", url=feed_url(base), error=str(exc))
        return SessionCheckResult(logged_in=False, reason="error", url=feed_url(base))
    log.debug("scraper_session_checked", logged_in=result.logged_in, reason=result.reason, url=result.url)
    return result
