"""LinkedInSearcher: content search per keyword on the persistent profile.

Slow and human-like: a random delay (``Pacer``) before every page action, at most
``SALES0_SCRAPE_MAX_SCROLLS`` scrolls and ``SALES0_SCRAPE_MAX_POSTS_PER_KEYWORD`` posts.
Any logout/checkpoint/captcha/rate-limit page closes the browser at once and raises
``AccountSafetyStop`` — never retried, never pushed through. A 429/999 on ANY LinkedIn
response (including background XHR/fetch such as ``/voyager/api/...``) counts as a rate
limit; it is recorded by a context-wide response listener and checked at every checkpoint
(after navigation, after each scroll, after each "see more" click).
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from typing import Any

from playwright.sync_api import BrowserContext, Locator, Page, Response
from playwright.sync_api import Error as PlaywrightError

from sales0.clock import local_today
from sales0.config import Settings
from sales0.logging import get_logger
from sales0.scraper import selectors
from sales0.scraper.browser import (
    first_page,
    open_context,
    profile_dir,
    run_headless,
    scroll_settle_ms,
    wait_timeout_ms,
)
from sales0.scraper.detection import classify, is_rate_limit_response, read_signals
from sales0.scraper.errors import AccountSafetyStop, ScrapeError
from sales0.scraper.pacing import Pacer
from sales0.scraper.parsing import EXTRACT_ARGS, EXTRACT_JS, ParseError, post_from_raw
from sales0.scraper.session_check import REASON_TO_KIND, verify_logged_in
from sales0.scraper.types import KeywordScrapeResult, ScrapedPost, SessionCheckResult
from sales0.scraper.urls import base_url, search_url

log = get_logger("sales0.scraper.search")

_POLL_MS = 250


class LinkedInSearcher:
    def __init__(self, settings: Settings, pacer: Pacer | None = None) -> None:
        self._settings = settings
        self._pacer = pacer or Pacer.for_actions(settings)
        self._base = base_url(settings)
        self._stack: ExitStack | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self.session: SessionCheckResult | None = None
        self._rate_limited_url: str | None = None  # first throttled LinkedIn response seen

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self) -> LinkedInSearcher:
        stack = ExitStack()
        try:
            context = stack.enter_context(open_context(profile_dir(self._settings), run_headless(self._settings)))
            self._stack, self._context = stack, context
            self._rate_limited_url = None
            context.on("response", self._on_response)
            self.session = verify_logged_in(context, self._base, wait_timeout_ms(self._settings))
            if self._rate_limited_url is not None:
                log.warning("scraper_safety_stop", kind="rate_limited", url=self._rate_limited_url, stage="session_check")
                raise AccountSafetyStop("rate_limited", self._rate_limited_url)
            if not self.session.logged_in:
                if self.session.reason == "error":
                    raise ScrapeError(f"Could not load the LinkedIn feed at {self.session.url}")
                kind = REASON_TO_KIND[self.session.reason]
                log.warning("scraper_safety_stop", kind=kind, url=self.session.url, stage="session_check")
                raise AccountSafetyStop(kind, self.session.url)
            self._page = first_page(context)
        except BaseException:
            self.close()
            stack.close()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._stack is not None

    def close(self) -> None:
        """Close the browser now. Idempotent."""
        stack, self._stack, self._context, self._page = self._stack, None, None, None
        if stack is not None:
            stack.close()

    # ------------------------------------------------------------------ safety
    def _stop(self, kind: Any, url: str) -> AccountSafetyStop:
        log.warning("scraper_safety_stop", kind=kind, url=url, stage="search")
        self.close()
        return AccountSafetyStop(kind, url)

    def _on_response(self, response: Response) -> None:
        """Context-wide listener: remember the first 429/999 from LinkedIn (document or background)."""
        if self._rate_limited_url is not None:
            return
        try:
            url, status = response.url, response.status
        except PlaywrightError:
            return
        if is_rate_limit_response(url, status, self._base):
            self._rate_limited_url = url
            log.warning("scraper_rate_limit_response", url=url, status=status)

    def _check_safety(self, page: Page, response: Response | None) -> None:
        if self._rate_limited_url is not None:
            raise self._stop("rate_limited", self._rate_limited_url)
        try:
            signals = read_signals(page, response)
        except PlaywrightError:
            # Usually a navigation in flight (e.g. a redirect to a checkpoint): let it land, re-read once.
            try:
                page.wait_for_load_state("domcontentloaded", timeout=wait_timeout_ms(self._settings))
                signals = read_signals(page, response)
            except PlaywrightError as exc:
                if self._rate_limited_url is not None:
                    raise self._stop("rate_limited", self._rate_limited_url) from exc
                raise ScrapeError(f"Could not read the LinkedIn page: {exc}") from exc
        if self._rate_limited_url is not None:
            raise self._stop("rate_limited", self._rate_limited_url)
        kind = classify(signals)
        if kind is None and not signals.has_logged_in_marker:
            kind = "logout"  # logged-out pages never carry the global nav
        if kind is not None:
            raise self._stop(kind, signals.url)

    # ------------------------------------------------------------------ search
    def search(self, keyword: str, max_posts: int) -> KeywordScrapeResult:
        page = self._page
        if page is None or not self.is_open:
            raise ScrapeError("LinkedInSearcher is not open (use it as a context manager)")
        cap = max(0, min(max_posts, self._settings.scrape_max_posts_per_keyword))
        url = search_url(self._base, keyword, self._settings.scrape_date_filter)
        started = time.monotonic()

        self._pacer.delay()
        try:
            response = page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            self._check_safety(page, None)
            raise ScrapeError(f"Could not open search results for '{keyword}': {exc}") from exc
        self._check_safety(page, response)
        if response is not None and response.status >= 400:
            raise ScrapeError(f"LinkedIn returned HTTP {response.status} for '{keyword}'")

        content_ready = f"{selectors.RESULT_CARD}, {selectors.NO_RESULTS_MARKER}"
        try:
            page.wait_for_selector(content_ready, state="attached", timeout=wait_timeout_ms(self._settings))
        except PlaywrightError:
            pass  # neither appeared: possible layout drift, decided below
        self._check_safety(page, None)

        cards = page.locator(selectors.RESULT_CARD)
        no_results = page.locator(selectors.NO_RESULTS_MARKER).count() > 0
        if cards.count() == 0:
            drift = not no_results
            if drift:
                log.warning("scraper_layout_drift", keyword=keyword, url=page.url)
            return KeywordScrapeResult(keyword=keyword, posts=[], scrolls=0, parse_errors=0, drift_warning=drift)

        scrolls = 0
        while cards.count() < cap and scrolls < self._settings.scrape_max_scrolls:
            before = cards.count()
            self._pacer.delay()
            self._wheel(page, self._pacer.randint(700, 1400))
            scrolls += 1
            grew = self._wait_for_more_cards(page, before)
            self._check_safety(page, None)
            if not grew:
                break  # end of results

        self._expand(page, min(cap, cards.count()))
        self._check_safety(page, None)
        posts, parse_errors = self._parse(page, cap)
        log.debug(
            "scraper_search_done",
            keyword=keyword,
            cards=cards.count(),
            posts=len(posts),
            scrolls=scrolls,
            parse_errors=parse_errors,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return KeywordScrapeResult(
            keyword=keyword, posts=posts, scrolls=scrolls, parse_errors=parse_errors, drift_warning=False
        )

    # Raw page actions, kept separate so tests can observe that a paced delay precedes each.
    def _wheel(self, page: Page, delta_y: int) -> None:
        try:
            page.mouse.wheel(0, delta_y)
        except PlaywrightError as exc:
            raise ScrapeError(f"Page closed while scrolling: {exc}") from exc

    def _click(self, button: Locator) -> None:
        button.click(timeout=3_000)

    def _wait_for_more_cards(self, page: Page, before: int) -> bool:
        """Poll up to ``scroll_settle_ms`` for more cards than ``before``. False = end of results
        (or the page navigated away / LinkedIn throttled us — the next safety check decides)."""
        deadline = time.monotonic() + scroll_settle_ms(self._settings) / 1000
        while True:
            try:
                page.wait_for_timeout(_POLL_MS)  # also pumps Playwright events (response listener)
            except PlaywrightError as exc:
                raise ScrapeError(f"Page closed while scrolling: {exc}") from exc
            if self._rate_limited_url is not None:
                return False
            try:
                if page.locator(selectors.RESULT_CARD).count() > before:
                    return True
            except PlaywrightError:
                return False  # mid-navigation (e.g. redirected to a checkpoint)
            if time.monotonic() >= deadline:
                return False

    def _expand(self, page: Page, n: int) -> None:
        """Click 'see more' on the first ``n`` cards, pacing each click like a human and
        re-checking account safety after each one."""
        cards = page.locator(selectors.RESULT_CARD)
        for i in range(n):
            button = cards.nth(i).locator(selectors.SEE_MORE_BUTTON)
            try:
                if button.count() == 0 or not button.first.is_visible():
                    continue
            except PlaywrightError:
                continue
            self._pacer.delay()
            try:
                self._click(button.first)
            except PlaywrightError:
                pass  # an unexpandable card is parsed as-is
            self._check_safety(page, None)

    def _parse(self, page: Page, cap: int) -> tuple[list[ScrapedPost], int]:
        try:
            raws: list[dict[str, Any]] = page.eval_on_selector_all(selectors.RESULT_CARD, EXTRACT_JS, EXTRACT_ARGS)
        except PlaywrightError as exc:
            raise ScrapeError(f"Could not read result cards: {exc}") from exc
        today = local_today(self._settings.tz)
        posts: list[ScrapedPost] = []
        seen: set[str] = set()
        errors = 0
        for raw in raws:
            if len(posts) >= cap:
                break
            try:
                post = post_from_raw(raw, self._base, today)
            except ParseError as exc:
                errors += 1
                log.debug("scraper_parse_error", error=str(exc))
                continue
            if post.urn in seen:
                continue
            seen.add(post.urn)
            posts.append(post)
        return posts, errors
