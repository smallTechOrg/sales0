"""Persistent Chromium contexts on the dedicated profile under the gitignored data dir."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from sales0.config import Settings
from sales0.logging import get_logger
from sales0.scraper.errors import ScrapeError

log = get_logger("sales0.scraper.browser")

LIVE_PROFILE_DIRNAME = "linkedin-profile"
FIXTURE_PROFILE_DIRNAME = "linkedin-profile-fixture"
_VIEWPORT = {"width": 1280, "height": 900}
_ARGS = ["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"]


def profile_dir(settings: Settings) -> Path:
    name = FIXTURE_PROFILE_DIRNAME if settings.linkedin_mode == "fixture" else LIVE_PROFILE_DIRNAME
    return settings.data_path / name


def run_headless(settings: Settings) -> bool:
    """Fixture mode is always headless; live runs follow SALES0_LINKEDIN_HEADLESS (default headed)."""
    return True if settings.linkedin_mode == "fixture" else settings.linkedin_headless


def wait_timeout_ms(settings: Settings) -> int:
    """How long to wait for page content to render (LinkedIn is a slow SPA; the fixture is not)."""
    return 5_000 if settings.linkedin_mode == "fixture" else 15_000


def scroll_settle_ms(settings: Settings) -> int:
    """How long to wait for new result cards after a scroll before concluding the end.

    LinkedIn lazy-loads the next page of results over XHR, which routinely takes 1-2s; the
    fixture reveals its lazy cards well within its shorter window.
    """
    return 1_200 if settings.linkedin_mode == "fixture" else 2_500


@contextmanager
def open_context(profile: Path, headless: bool) -> Iterator[BrowserContext]:
    """Open (and always close) a persistent Chromium context on ``profile``.

    Raises ``ScrapeError`` when the browser cannot start (e.g. the profile is locked by
    another Chromium, or ``playwright install chromium`` was never run).
    """
    profile.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    try:
        try:
            context = pw.chromium.launch_persistent_context(
                str(profile),
                headless=headless,
                viewport=_VIEWPORT,
                locale="en-US",
                args=_ARGS,
                chromium_sandbox=True,  # keep Chromium's OS sandbox on (Playwright disables it by default)
            )
        except PlaywrightError as exc:
            raise ScrapeError(
                f"Could not open the LinkedIn browser profile at {profile} — is another browser using it? ({exc})"
            ) from exc
        log.debug("browser_opened", profile=str(profile), headless=headless)
        try:
            yield context
        finally:
            try:
                context.close()
            except PlaywrightError:
                pass  # the user may already have closed the window
            log.debug("browser_closed", profile=str(profile))
    finally:
        pw.stop()


def first_page(context: BrowserContext) -> Page:
    """The profile's initial tab (persistent contexts open with one), or a new one."""
    for page in context.pages:
        if not page.is_closed():
            return page
    return context.new_page()
