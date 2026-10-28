"""Session check: is sales0's browser profile logged in to LinkedIn? Never logs in.

Detection uses the ``li_at`` session cookie, the final URL and the main-document status via
``detection.classify_page``; DOM markers for logged-in pages are soft hints only
(architecture.md -> ``## LinkedIn browser`` -> Logged-in detection).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import Error as PlaywrightError

from sales0.config import Settings
from sales0.logging import get_logger
from sales0.scraper.browser import first_page, open_context, resolve_browser, wait_timeout_ms
from sales0.scraper.detection import PageVerdict, classify_page, read_signals, session_cookie_present
from sales0.scraper.session_store import AccountHandle, open_account_context
from sales0.scraper.types import SessionCheckResult, SessionReason
from sales0.scraper.urls import base_url, feed_url

log = get_logger("sales0.scraper.session")

# Wait for "something rendered" after the feed load; missing content never fails the check.
CONTENT_READY_SELECTOR = 'main, [role="main"], form, iframe'

_KIND_TO_REASON: dict[str, SessionReason] = {
    "ok": "ok",
    "checkpoint": "checkpoint",
    "captcha": "captcha",
    "rate_limited": "rate_limited",
    "http_error": "error",
}


def reason_for(verdict: PageVerdict) -> SessionReason:
    """Map a page verdict to a ``SessionCheckResult.reason``."""
    if verdict.kind == "logout":
        return "no_cookie" if verdict.reason == "no_cookie" else "redirected_login"
    return _KIND_TO_REASON.get(verdict.kind, "error")


@dataclass(frozen=True)
class CheckDetails:
    """Log-only facts about one verification (never cookie values)."""

    cookie_present: bool
    final_path: str
    status_code: int | None
    verdict: str | None


def _path(url: str) -> str:
    try:
        return urlsplit(url).path or "/"
    except ValueError:
        return ""


def verify_with_details(
    context: BrowserContext, base: str, timeout_ms: int, page: Page | None = None
) -> tuple[SessionCheckResult, CheckDetails]:
    """``verify_in_context`` plus the log-only details (cookie presence, final path, status)."""
    url = feed_url(base)
    if not session_cookie_present(context, base):
        return (
            SessionCheckResult(logged_in=False, reason="no_cookie", url=url),
            CheckDetails(cookie_present=False, final_path=_path(url), status_code=None, verdict=None),
        )
    target = page if page is not None and not page.is_closed() else first_page(context)
    try:
        response = target.goto(url, wait_until="domcontentloaded")
    except PlaywrightError as exc:
        log.warning("scraper_session_error", final_path=_path(url), error=str(exc).splitlines()[0][:200])
        return (
            SessionCheckResult(logged_in=False, reason="error", url=url),
            CheckDetails(cookie_present=True, final_path=_path(url), status_code=None, verdict="error"),
        )
    try:
        target.wait_for_selector(CONTENT_READY_SELECTOR, state="attached", timeout=timeout_ms)
    except PlaywrightError:
        pass  # missing content is not a failure; the classifier decides
    try:
        signals, _extras = read_signals(target, response, context, base)
    except PlaywrightError as exc:
        log.warning("scraper_session_error", final_path=_path(target.url), error=str(exc).splitlines()[0][:200])
        return (
            SessionCheckResult(logged_in=False, reason="error", url=target.url),
            CheckDetails(cookie_present=True, final_path=_path(target.url), status_code=None, verdict="error"),
        )
    verdict = classify_page(signals)
    reason = reason_for(verdict)
    result = SessionCheckResult(
        logged_in=reason == "ok", reason=reason, url=signals.url, soft_hints=tuple(signals.soft_hints)
    )
    details = CheckDetails(
        cookie_present=signals.has_session_cookie,
        final_path=_path(signals.url),
        status_code=signals.status,
        verdict=f"{verdict.kind}:{verdict.reason}" if verdict.reason else verdict.kind,
    )
    if result.logged_in and not result.soft_hints:
        log.warning("linkedin_dom_hints_missing", final_path=details.final_path)
    return result, details


def verify_in_context(
    context: BrowserContext, base: str, timeout_ms: int, page: Page | None = None
) -> SessionCheckResult:
    """Verify an open context: no navigation when ``li_at`` is absent (reason ``no_cookie``);
    otherwise ONE ``goto {base}/feed/`` -> ``read_signals`` -> ``classify_page``."""
    return verify_with_details(context, base, timeout_ms, page)[0]


def log_session(
    action: str,
    settings: Settings,
    result: SessionCheckResult,
    details: CheckDetails,
    *,
    headless: bool,
) -> None:
    """The ``linkedin_session`` log line with the Phase 1.1 browser/detection fields."""
    try:
        choice = resolve_browser(settings)
        browser, browser_version = choice.channel, choice.version
    except Exception:  # noqa: BLE001 — logging must never fail the check
        browser, browser_version = None, None
    log.info(
        "linkedin_session",
        action=action,
        result="connected" if result.logged_in else "not_connected",
        reason=result.reason,
        browser=browser,
        browser_version=browser_version,
        headless=headless,
        cookie_present=details.cookie_present,
        final_path=details.final_path,
        status_code=details.status_code,
        soft_hints=list(result.soft_hints),
        verdict=details.verdict,
    )


def check_session(settings: Settings, account: AccountHandle | None = None) -> SessionCheckResult:
    """Re-check: open sales0's profile HEADLESS, verify the feed, close.

    ``account`` picks the LinkedIn account's session store (None = the default folder profile); a db
    account's refreshed storage_state is saved back when the context closes.

    ``BrowserUnavailable`` (Chrome required but missing, profile window still open, launch
    failure) propagates so the job can show its message. Any other browser failure during
    the check returns reason ``error``.
    """
    base = base_url(settings)
    opened = open_context(settings, headless=True) if account is None else open_account_context(
        settings, account, headless=True
    )
    with opened as context:
        try:
            result, details = verify_with_details(context, base, wait_timeout_ms(settings))
        except PlaywrightError as exc:
            log.warning("scraper_session_error", final_path=_path(feed_url(base)), error=str(exc).splitlines()[0][:200])
            result = SessionCheckResult(logged_in=False, reason="error", url=feed_url(base))
            details = CheckDetails(cookie_present=False, final_path=_path(feed_url(base)), status_code=None, verdict="error")
    log_session("check", settings, result, details, headless=True)
    return result
