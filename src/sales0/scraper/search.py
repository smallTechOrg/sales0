"""LinkedInSearcher: content search per keyword on sales0's own (headless) browser profile.

Slow and human-like: a random delay (``Pacer``) before every page action, at most
``SALES0_SCRAPE_MAX_SCROLLS`` scrolls and ``SALES0_SCRAPE_MAX_POSTS_PER_KEYWORD`` posts.

Safety (architecture.md → "Mid-run safety"):
- **Latches**, registered at ``__enter__`` before the run-start check: a context-wide
  ``response`` listener (any same-site 429/999, including background ``/voyager/api/``
  calls) and a main-frame ``framenavigated`` listener (``classify_url`` not None, e.g. a
  redirect to ``/authwall``, ``/login`` or ``/checkpoint/…``).
- **Checkpoints** before each keyword, after each navigation, scroll and "see more" click,
  and (latch only) on every poll while waiting for more cards: latch → stop; else
  ``read_signals`` → ``classify_page``. ``ok`` continues; ``http_error`` on the search
  document is a keyword ``ScrapeError``; anything else is a stop.
- **Stop** = close the browser FIRST, then raise ``AccountSafetyStop``. Never retried.

Layout drift is never silent: a logged-in results page that has result cards, or lacks the
"no results" marker, but yields 0 posts sets ``drift_warning`` and logs WARNING
``scraper_layout_drift``. Missing optional fields are counted into ``field_misses``.

Phase 1.2 (SDUI results): cards come from ONE ordered strategy (``sdui`` → ``urn`` → ``none``,
in ``parsing.py``). After "see more" and ``EXTRACT_JS``, ``permalink.capture_permalinks`` tries
"Copy link to post" on posts without a URN (paced, checkpointed, capped by
``SALES0_SCRAPE_PERMALINK_MAX_PER_KEYWORD``; the system clipboard is never touched). Before each
scroll wheel the mouse is moved over the SDUI lazy column when present.
"""

from __future__ import annotations

import time
from collections import Counter
from contextlib import ExitStack
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, ElementHandle, Frame, Page, Response
from playwright.sync_api import Error as PlaywrightError

from sales0.clock import local_today
from sales0.config import Settings
from sales0.logging import get_logger
from sales0.scraper.browser import first_page, open_context
from sales0.scraper.detection import (
    CONTENT_READY_JS,
    SIGNAL_ARGS,
    classify_page,
    classify_url,
    is_rate_limit_response,
    read_signals,
)
from sales0.scraper.errors import AccountSafetyStop, SafetyKind, ScrapeError, SessionMissing
from sales0.scraper import selectors as S
from sales0.scraper.pacing import Pacer
from sales0.scraper.parsing import (
    CARD_ARGS,
    COUNT_CARDS_JS,
    EXTRACT_ARGS,
    EXTRACT_JS,
    SEE_MORE_JS,
    drift_detected,
    parse_cards,
    see_more_args,
)
from sales0.scraper.permalink import PermalinkStats, capture_permalinks
from sales0.scraper.session_check import verify_in_context
from sales0.scraper.types import KeywordScrapeResult, SessionCheckResult
from sales0.scraper.urls import base_url, search_url

log = get_logger("sales0.scraper.search")

_POLL_MS = 250

# verify_in_context reason → the stop it means at run start (no_cookie / error handled separately).
_SESSION_STOP: dict[str, SafetyKind] = {
    "redirected_login": "logout",
    "checkpoint": "checkpoint",
    "captcha": "captcha",
    "rate_limited": "rate_limited",
}


def content_timeout_ms(settings: Settings) -> int:
    """How long to wait for page content (LinkedIn is a slow SPA; the fixture is not)."""
    return 5_000 if settings.linkedin_mode == "fixture" else 15_000


def scroll_settle_ms(settings: Settings) -> int:
    """How long to wait for more cards after a scroll before concluding the end of results."""
    return 1_200 if settings.linkedin_mode == "fixture" else 2_500


# The point to hover before a wheel: the centre of the visible part of the SDUI lazy column
# (null when there is none, or it is off-screen).
SCROLL_TARGET_JS = r"""(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  const top = Math.max(r.top, 0), bottom = Math.min(r.bottom, window.innerHeight);
  const left = Math.max(r.left, 0), right = Math.min(r.right, window.innerWidth);
  if (bottom - top < 2 || right - left < 2) return null;
  return { x: (left + right) / 2, y: (top + bottom) / 2 };
}"""

SDUI_SCREEN_JS = "(sel) => !!document.querySelector(sel)"


def extract_raw_cards(page: Page) -> list[dict[str, Any]]:
    """Run ``EXTRACT_JS`` on the page: one raw dict per discovered card."""
    raws = page.evaluate(EXTRACT_JS, EXTRACT_ARGS)
    return list(raws or [])


def count_cards(page: Page) -> int:
    return int(page.evaluate(COUNT_CARDS_JS, CARD_ARGS) or 0)


def see_more_buttons(page: Page, n: int) -> list[ElementHandle]:
    """The "see more" toggle of each of the first ``n`` cards (cards without one are skipped)."""
    handle = page.evaluate_handle(SEE_MORE_JS, see_more_args(n))
    try:
        elements = [prop.as_element() for prop in handle.get_properties().values()]
    finally:
        handle.dispose()
    return [el for el in elements if el is not None]


class LinkedInSearcher:
    def __init__(self, settings: Settings, pacer: Pacer | None = None) -> None:
        self._settings = settings
        self._pacer = pacer or Pacer.for_actions(settings)
        self._base = base_url(settings)
        self._stack: ExitStack | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self.session: SessionCheckResult | None = None
        self._latch: tuple[SafetyKind, str] | None = None  # first safety signal seen by a listener

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self) -> LinkedInSearcher:
        stack = ExitStack()
        try:
            context = stack.enter_context(open_context(self._settings, headless=True))
            self._stack, self._context = stack, context
            self._latch = None
            page = first_page(context)
            self._page = page
            context.on("response", self._on_response)
            page.on("framenavigated", self._on_navigated)
            self.session = verify_in_context(context, self._base, content_timeout_ms(self._settings), page=page)
            self._enter_verdict(self.session)
        except BaseException:
            self.close()
            stack.close()
            raise
        return self

    def _enter_verdict(self, session: SessionCheckResult) -> None:
        if session.reason == "no_cookie":
            log.warning("scraper_session_missing", url=session.url)
            self.close()
            raise SessionMissing(session.url)
        if self._latch is not None:
            raise self._stop(*self._latch, stage="session_check")
        if session.logged_in:
            log.debug("scraper_session_ok", url=session.url, soft_hints=list(session.soft_hints))
            return
        if session.reason == "error":
            self.close()
            raise ScrapeError(f"Could not load the LinkedIn feed at {session.url}")
        raise self._stop(_SESSION_STOP[session.reason], session.url, stage="session_check")

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
    def _stop(self, kind: SafetyKind, url: str, stage: str = "search") -> AccountSafetyStop:
        log.warning("scraper_safety_stop", kind=kind, url=url, stage=stage)
        self.close()  # close FIRST, then the caller raises
        return AccountSafetyStop(kind, url)

    def _set_latch(self, kind: SafetyKind, url: str) -> None:
        if self._latch is None:
            self._latch = (kind, url)

    def _on_response(self, response: Response) -> None:
        """Context-wide: any same-site 429/999 (document or background) latches ``rate_limited``."""
        if self._latch is not None:
            return
        try:
            url, status = response.url, response.status
        except PlaywrightError:
            return
        if is_rate_limit_response(url, status, self._base):
            log.warning("scraper_rate_limit_response", url=url, status=status)
            self._set_latch("rate_limited", url)

    def _on_navigated(self, frame: Frame) -> None:
        """Main frame only: a navigation to a login/checkpoint/captcha path latches that kind."""
        try:
            if frame.parent_frame is not None:
                return
            url = frame.url
        except PlaywrightError:
            return
        kind = classify_url(url, self._base)
        if kind is not None:
            log.warning("scraper_safety_navigation", kind=kind, url=url)
            self._set_latch(kind, url)

    def _raise_if_latched(self) -> None:
        if self._latch is not None:
            raise self._stop(*self._latch)

    def _checkpoint(self, page: Page, response: Response | None = None, *, what: str = "") -> dict:
        """Latch → stop; else classify the page. Returns the SIGNALS_JS extras on ``ok``."""
        self._raise_if_latched()
        context = self._context
        if context is None:
            raise ScrapeError("LinkedInSearcher is closed")
        try:
            signals, extras = read_signals(page, response, context, self._base)
        except PlaywrightError:
            # Usually a navigation in flight (e.g. a redirect to a checkpoint): let it land, re-read once.
            try:
                page.wait_for_load_state("domcontentloaded", timeout=content_timeout_ms(self._settings))
                signals, extras = read_signals(page, response, context, self._base)
            except PlaywrightError as exc:
                self._raise_if_latched()
                raise ScrapeError(f"Could not read the LinkedIn page: {exc}") from exc
        self._raise_if_latched()
        verdict = classify_page(signals)
        if verdict.kind == "ok":
            return extras
        if verdict.kind == "http_error":
            raise ScrapeError(f"LinkedIn returned HTTP {signals.status}{what}")
        log.debug("scraper_checkpoint_verdict", kind=verdict.kind, reason=verdict.reason, url=signals.url)
        raise self._stop(verdict.kind, signals.url)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ search
    def search(self, keyword: str, max_posts: int) -> KeywordScrapeResult:
        page = self._page
        if page is None or not self.is_open:
            raise ScrapeError("LinkedInSearcher is not open (use it as a context manager)")
        cap = max(0, min(max_posts, self._settings.scrape_max_posts_per_keyword))
        url = search_url(self._base, keyword, self._settings.scrape_date_filter)  # ValueError on a blank keyword
        started = time.monotonic()
        what = f" for '{keyword}'"

        self._checkpoint(page, what=what)  # before each keyword (e.g. li_at gone since the last one)
        self._pacer.delay()
        try:
            response = page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            self._raise_if_latched()
            raise ScrapeError(f"Could not open search results{what}: {exc}") from exc
        self._checkpoint(page, response, what=what)

        try:
            page.wait_for_function(CONTENT_READY_JS, arg=SIGNAL_ARGS, timeout=content_timeout_ms(self._settings))
        except PlaywrightError:
            pass  # nothing appeared: possible layout drift, decided below
        extras = self._checkpoint(page, what=what)
        cards_found, no_results = extras["cards_found"], extras["no_results"]

        scrolls = 0
        if cards_found > 0:
            while cards_found < cap and scrolls < self._settings.scrape_max_scrolls:
                before = cards_found
                self._pacer.delay()
                self._hover_scroll_column(page)
                self._wheel(page, self._pacer.randint(700, 1400))
                scrolls += 1
                grew = self._wait_for_more_cards(page, before)
                extras = self._checkpoint(page, what=what)
                cards_found, no_results = extras["cards_found"], extras["no_results"]
                if not grew:
                    break  # end of results
            self._expand(page, min(cap, cards_found), what)

        try:
            raws = extract_raw_cards(page) if cards_found > 0 else []
        except PlaywrightError as exc:
            self._raise_if_latched()
            raise ScrapeError(f"Could not read result cards{what}: {exc}") from exc
        stats = PermalinkStats()
        permalink_cap = self._settings.scrape_permalink_max_per_keyword
        if raws and permalink_cap > 0:
            # Eligible = the first `cap` raws (DOM order); a slice keeps the same dicts, so hits
            # land on `raws` itself.
            stats = capture_permalinks(
                page,
                raws[:cap],
                cap=permalink_cap,
                pacer=self._pacer,
                checkpoint=lambda: self._checkpoint(page, what=what),
                keyword=keyword,
            )
        parsed = parse_cards(raws, self._base, local_today(self._settings.tz), cap, search_url=url)
        cards_found = max(cards_found, parsed.cards_found)
        drift = drift_detected(len(parsed.posts), cards_found, no_results)
        for error in parsed.errors:
            log.debug("scraper_parse_error", keyword=keyword, error=error)
        if raws:
            via = Counter(f"{k}={v}" for raw in raws for k, v in (raw.get("via") or {}).items())
            log.debug("scraper_parse_via", keyword=keyword, via=dict(via))
        if drift:
            log.warning(
                "scraper_layout_drift",
                keyword=keyword,
                final_path=urlsplit(page.url).path,
                cards_found=cards_found,
                parse_errors=parsed.parse_errors,
            )
        log.debug(
            "scraper_search_done",
            keyword=keyword,
            strategy=parsed.strategy,
            sdui_screen=self._sdui_screen(page),
            permalink_attempts=stats.attempts,
            permalinks_found=stats.found,
            cards_found=cards_found,
            posts=len(parsed.posts),
            scrolls=scrolls,
            parse_errors=parsed.parse_errors,
            field_misses=parsed.field_misses,
            no_results=no_results,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return KeywordScrapeResult(
            keyword=keyword,
            posts=parsed.posts,
            scrolls=scrolls,
            parse_errors=parsed.parse_errors,
            drift_warning=drift,
            cards_found=cards_found,
            no_results=no_results,
            field_misses=parsed.field_misses,
            strategy=parsed.strategy,
            permalink_attempts=stats.attempts,
            permalinks_found=stats.found,
        )

    # Raw page actions, kept separate so tests can observe that a paced delay precedes each.
    def _wheel(self, page: Page, delta_y: int) -> None:
        try:
            page.mouse.wheel(0, delta_y)
        except PlaywrightError as exc:
            self._raise_if_latched()
            raise ScrapeError(f"Page closed while scrolling: {exc}") from exc

    def _hover_scroll_column(self, page: Page) -> None:
        """Move the mouse over the SDUI lazy column (if any) so the wheel scrolls it."""
        try:
            point = page.evaluate(SCROLL_TARGET_JS, S.SDUI_SCROLL_CONTAINER)
            if point:
                page.mouse.move(float(point["x"]), float(point["y"]), steps=self._pacer.randint(4, 12))
        except PlaywrightError:
            self._raise_if_latched()  # the wheel and the next checkpoint decide the rest

    def _sdui_screen(self, page: Page) -> bool:
        """Informational only (logged), never a verdict."""
        try:
            return bool(page.evaluate(SDUI_SCREEN_JS, S.SDUI_SCREEN))
        except PlaywrightError:
            return False

    def _click(self, button: ElementHandle) -> None:
        button.click(timeout=3_000)

    def _wait_for_more_cards(self, page: Page, before: int) -> bool:
        """Poll up to ``scroll_settle_ms`` for more cards than ``before`` (latch checked every poll).
        False = end of results, or the page is mid-navigation (the next checkpoint decides)."""
        deadline = time.monotonic() + scroll_settle_ms(self._settings) / 1000
        while True:
            try:
                page.wait_for_timeout(_POLL_MS)  # also pumps Playwright events (the latches)
            except PlaywrightError as exc:
                self._raise_if_latched()
                raise ScrapeError(f"Page closed while scrolling: {exc}") from exc
            self._raise_if_latched()
            try:
                if count_cards(page) > before:
                    return True
            except PlaywrightError:
                return False  # mid-navigation (e.g. redirected to a checkpoint)
            if time.monotonic() >= deadline:
                return False

    def _expand(self, page: Page, n: int, what: str) -> None:
        """Click "see more" on the first ``n`` cards, paced like a human, with a checkpoint after each."""
        if n <= 0:
            return
        try:
            buttons = see_more_buttons(page, n)
        except PlaywrightError:
            self._raise_if_latched()
            return
        for button in buttons:
            try:
                if not button.is_visible():
                    continue
            except PlaywrightError:
                continue
            self._pacer.delay()
            try:
                self._click(button)
            except PlaywrightError:
                pass  # an unexpandable card is parsed as-is
            self._checkpoint(page, what=what)
