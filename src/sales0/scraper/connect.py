"""Connect LinkedIn: open sales0's Google Chrome profile in a window, let the USER log in
(including 2FA and "remember this browser"), detect the logged-in session, close.
sales0 never types, reads or stores credentials.

Live mode is headed (the only headed launch); fixture mode is headless. The poll rule is
cheap and never navigates: the ``li_at`` cookie is present AND some open page is on a
same-site URL that is not a login/checkpoint/captcha path. Only then is the page verified
once on ``/feed/`` (architecture.md -> ``## LinkedIn browser`` -> Connect). Login, 2FA and
checkpoint pages during Connect are never stops; the user is expected to pass them.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import Error as PlaywrightError

from sales0.config import Settings
from sales0.logging import get_logger
from sales0.scraper.browser import first_page, open_context, resolve_browser, wait_timeout_ms
from sales0.scraper.detection import classify_url, same_site, session_cookie_present
from sales0.scraper.errors import BrowserUnavailable
from sales0.scraper.session_store import AccountHandle, open_account_context
from sales0.scraper.session_check import log_session, verify_with_details
from sales0.scraper.types import ConnectResult
from sales0.scraper.urls import base_url, login_url

log = get_logger("sales0.scraper.connect")

TIMEOUT_MESSAGE = "Login not completed — try again"
WINDOW_CLOSED_MESSAGE = "The LinkedIn window was closed before login finished — try again"
CANCELLED_MESSAGE = "Connect cancelled"
RATE_LIMITED_MESSAGE = "LinkedIn is rate-limiting this browser — wait a few hours, then Connect again"


def _open_pages(context: BrowserContext) -> list[Page]:
    return [p for p in context.pages if not p.is_closed()]


def logged_in_page_candidates(context: BrowserContext, base: str, pages: list[Page]) -> list[Page]:
    """The cheap rule: cookie present (no navigation) AND pages on a same-site, non-login,
    non-checkpoint URL. Empty while the user is on /login, /checkpoint/... or /authwall."""
    candidates = [p for p in pages if same_site(p.url, base) and classify_url(p.url) is None]
    if not candidates:
        return []
    return candidates if session_cookie_present(context, base) else []


def connect_interactive(
    settings: Settings,
    on_tick: Callable[[int], None],
    should_cancel: Callable[[], bool],
    *,
    poll_interval_s: float = 1.0,
    timeout_s: float | None = None,
    start_url: str | None = None,
    account: AccountHandle | None = None,
) -> ConnectResult:
    """Block until the profile is logged in, the timeout passes, the user cancels or closes
    the window. ``on_tick(elapsed_s)`` is called about once per ``poll_interval_s``.

    ``start_url`` (default ``{base}/login``) exists for tests. ``account`` selects the LinkedIn account's
    session store (None = the default folder profile). Only folder accounts can Connect via a window.
    """
    base = base_url(settings)
    headless = settings.linkedin_mode == "fixture"
    timeout = float(timeout_s if timeout_s is not None else settings.linkedin_connect_timeout_s)
    target = start_url or login_url(base)
    closed = {"value": False}
    try:
        choice = resolve_browser(settings)
        opened = (
            open_context(settings, headless=headless)
            if account is None
            else open_account_context(settings, account, headless=headless)
        )
        with opened as context:
            context.on("close", lambda _ctx: closed.update(value=True))
            started = time.monotonic()  # the user's time starts once the window is open
            page = first_page(context)
            try:
                page.goto(target, wait_until="domcontentloaded")
            except PlaywrightError as exc:
                # The user may already be interacting with the window; keep polling.
                log.debug("scraper_connect_nav_interrupted", error=str(exc).splitlines()[0][:200])
            # page -> URL after its last failed verification (not re-verified until it changes)
            failed_at: dict[int, str] = {}
            while True:
                pages = [] if closed["value"] else _open_pages(context)
                if not pages:
                    return ConnectResult(connected=False, reason="window_closed", detail=WINDOW_CLOSED_MESSAGE)
                if should_cancel():
                    return ConnectResult(connected=False, reason="cancelled", detail=CANCELLED_MESSAGE)
                elapsed = time.monotonic() - started
                if elapsed >= timeout:
                    return ConnectResult(connected=False, reason="timeout", detail=TIMEOUT_MESSAGE)
                on_tick(int(elapsed))
                for candidate in logged_in_page_candidates(context, base, pages):
                    if failed_at.get(id(candidate)) == candidate.url:
                        continue
                    result, details = verify_with_details(context, base, wait_timeout_ms(settings), page=candidate)
                    if result.logged_in:
                        log_session("connect", settings, result, details, headless=headless)
                        return ConnectResult(connected=True, reason="ok", detail=None, browser=choice.channel)
                    if result.reason == "rate_limited":
                        log_session("connect", settings, result, details, headless=headless)
                        return ConnectResult(
                            connected=False, reason="error", detail=RATE_LIMITED_MESSAGE, browser=choice.channel
                        )
                    log.debug("scraper_connect_verify_failed", reason=result.reason, final_path=details.final_path)
                    failed_at[id(candidate)] = "" if candidate.is_closed() else candidate.url
                    break
                try:
                    live = _open_pages(context)
                    (live[0] if live else pages[0]).wait_for_timeout(poll_interval_s * 1000)  # pumps events
                except PlaywrightError:
                    continue  # that page closed; the next loop re-reads the open pages
    except BrowserUnavailable as exc:
        log.warning("scraper_connect_error", error=str(exc))
        return ConnectResult(connected=False, reason="error", detail=str(exc))
    except PlaywrightError as exc:
        if closed["value"] or "closed" in str(exc).lower():
            return ConnectResult(connected=False, reason="window_closed", detail=WINDOW_CLOSED_MESSAGE)
        log.warning("scraper_connect_error", error=str(exc).splitlines()[0][:200])
        return ConnectResult(connected=False, reason="error", detail=str(exc).splitlines()[0][:200])
