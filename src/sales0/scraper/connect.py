"""Connect LinkedIn: open the dedicated profile in a window, let the USER log in, detect the
logged-in feed, close. sales0 never types, reads or stores credentials.

Live mode is always headed. Fixture mode is headless: the fixture ``/login`` route sets the
session cookie itself. Checkpoint/2FA pages during connect are NOT stops — the user is
expected to pass them in the window — so this only polls for the logged-in marker.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import Error as PlaywrightError

from sales0.config import Settings
from sales0.logging import get_logger
from sales0.scraper.browser import first_page, open_context, profile_dir
from sales0.scraper.detection import classify, read_signals
from sales0.scraper.errors import ScrapeError
from sales0.scraper.types import ConnectResult
from sales0.scraper.urls import base_url, login_url

log = get_logger("sales0.scraper.connect")

TIMEOUT_MESSAGE = "Login not completed — try again"
WINDOW_CLOSED_MESSAGE = "The LinkedIn window was closed before login finished — try again"
CANCELLED_MESSAGE = "Connect cancelled"


def _page_logged_in(page: Page) -> bool:
    try:
        if page.is_closed():
            return False
        signals = read_signals(page, None)
    except PlaywrightError:  # mid-navigation: execution context destroyed, etc.
        return False
    return signals.has_logged_in_marker and classify(signals) is None


def _open_pages(context: BrowserContext) -> list[Page]:
    return [p for p in context.pages if not p.is_closed()]


def connect_interactive(
    settings: Settings,
    on_tick: Callable[[int], None],
    should_cancel: Callable[[], bool],
    *,
    poll_interval_s: float = 1.0,
    timeout_s: float | None = None,
    start_url: str | None = None,
) -> ConnectResult:
    """Block until the profile is logged in, the timeout passes, the user cancels or closes
    the window. ``on_tick(elapsed_s)`` is called about once per ``poll_interval_s``."""
    base = base_url(settings)
    headless = settings.linkedin_mode == "fixture"
    timeout = float(timeout_s if timeout_s is not None else settings.linkedin_connect_timeout_s)
    target = start_url or login_url(base)
    started = time.monotonic()
    closed = {"value": False}
    try:
        with open_context(profile_dir(settings), headless) as context:
            context.on("close", lambda _ctx: closed.update(value=True))
            page = first_page(context)
            try:
                page.goto(target, wait_until="domcontentloaded")
            except PlaywrightError as exc:
                # Navigation can be interrupted by the user; keep polling the window.
                log.debug("scraper_connect_nav_interrupted", error=str(exc))
            while True:
                pages = [] if closed["value"] else _open_pages(context)
                if not pages:
                    return ConnectResult(connected=False, reason="window_closed", detail=WINDOW_CLOSED_MESSAGE)
                if any(_page_logged_in(p) for p in pages):
                    log.debug("scraper_connect_ok", elapsed_s=int(time.monotonic() - started))
                    return ConnectResult(connected=True, reason="ok", detail=None)
                if should_cancel():
                    return ConnectResult(connected=False, reason="cancelled", detail=CANCELLED_MESSAGE)
                elapsed = time.monotonic() - started
                if elapsed >= timeout:
                    return ConnectResult(connected=False, reason="timeout", detail=TIMEOUT_MESSAGE)
                on_tick(int(elapsed))
                try:
                    pages[0].wait_for_timeout(poll_interval_s * 1000)  # pumps Playwright events
                except PlaywrightError:
                    continue  # that page closed; the next loop re-reads the open pages
    except ScrapeError as exc:
        return ConnectResult(connected=False, reason="error", detail=str(exc))
    except PlaywrightError as exc:
        if closed["value"] or "closed" in str(exc).lower():
            return ConnectResult(connected=False, reason="window_closed", detail=WINDOW_CLOSED_MESSAGE)
        log.warning("scraper_connect_error", error=str(exc))
        return ConnectResult(connected=False, reason="error", detail=str(exc))
