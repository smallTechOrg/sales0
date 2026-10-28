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

Phase 1.4 (thorough discovery): ``scan`` loops one pass per ``SALES0_SCRAPE_DATE_FILTERS`` entry
(sorted per ``SALES0_SCRAPE_SORT``), scrolling until the card target, ``SALES0_SCRAPE_MAX_SCROLLS`` or
``SALES0_SCRAPE_NO_GROWTH_SCROLLS`` idle scrolls, expands "see more" (``SALES0_SCRAPE_EXPAND_MAX_PER_KEYWORD``)
and never opens a menu. ``capture_links`` (after triage, same loaded page) copies permalinks for the
refs the caller asks for, on both the ``sdui`` and ``listitem`` tiers. ``search`` = ``scan`` +
``capture_links`` for old callers.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
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
from sales0.scraper.permalink import ref_of_raw, capture_permalinks, eligible_refs
from sales0.scraper.session_check import verify_in_context
from sales0.scraper.session_store import AccountHandle, open_account_context
from sales0.scraper.types import (
    CardStrategy,
    KeywordScrapeResult,
    LinkCaptureResult,
    ScrapedPost,
    SessionCheckResult,
    StopReason,
)
from sales0.scraper.variants import plan_passes
from sales0.scraper.urls import base_url, canonical_post_url, link_kind_of, search_url, urn_from_href

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
    def __init__(self, settings: Settings, pacer: Pacer | None = None, account: AccountHandle | None = None) -> None:
        self._settings = settings
        self._account = account  # None = the default folder profile
        self._pacer = pacer or Pacer.for_actions(settings)
        self._base = base_url(settings)
        self._stack: ExitStack | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self.session: SessionCheckResult | None = None
        self._latch: tuple[SafetyKind, str] | None = None  # first safety signal seen by a listener
        self._card_raws: dict[str, dict[str, Any]] = {}  # last scan(): source_ref -> raw card
        self._last_keyword = ""

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self) -> LinkedInSearcher:
        stack = ExitStack()
        try:
            opened = (
                open_context(self._settings, headless=True)
                if self._account is None
                else open_account_context(self._settings, self._account, headless=True)
            )
            context = stack.enter_context(opened)
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
    def scan(self, keyword: str, max_posts: int) -> KeywordScrapeResult:
        """Phase 1.4: read up to ``max_posts`` posts for ``keyword`` and NEVER open a post menu.

        One pass per configured date filter (sorted per ``scrape_sort``). A pass scrolls the results
        until the card target is met, ``scrape_max_scrolls`` is hit, or ``scrape_no_growth_scrolls``
        consecutive scrolls add no card; expands truncated cards (``scrape_expand_max_per_keyword``
        per keyword); extracts and parses. Posts are merged across passes by ``source_ref`` (first
        sighting wins). The last page stays loaded for ``capture_links``.
        """
        page = self._page
        if page is None or not self.is_open:
            raise ScrapeError("LinkedInSearcher is not open (use it as a context manager)")
        settings = self._settings
        cap = max(0, min(max_posts, settings.scrape_max_posts_per_keyword))
        self._last_keyword = keyword
        started = time.monotonic()
        what = f" for '{keyword}'"
        today = local_today(settings.tz)
        plan = plan_passes(
            keyword,
            tuple(settings.scrape_date_filters),
            settings.scrape_sort,
            variants=settings.scrape_variants and settings.linkedin_mode == "live",
            max_passes=settings.scrape_max_passes_per_keyword,
            today=today,
        )
        filters = tuple(p.date_filter for p in plan)
        urls = [search_url(self._base, p.query, p.date_filter, p.sort) for p in plan]  # ValueError on blank

        merged: dict[str, ScrapedPost] = {}
        pass_raws: list[list[dict[str, Any]]] = []
        scrolls_total = expanded_total = passes = parse_errors = cards_max = 0
        field_misses: dict[str, int] = {}
        strategy: CardStrategy = "none"
        stop: StopReason = "no_growth"
        any_drift = False
        no_results = False
        for date_filter, url, spec in zip(filters, urls, plan):
            if len(merged) >= cap and passes > 0:
                break
            passes += 1
            target = cap + len(merged)  # posts already merged show up again in later passes
            outcome = self._scan_pass(page, keyword, date_filter, url, target, expanded_total, what)
            scrolls_total += outcome.scrolls
            expanded_total += outcome.expanded
            no_results = outcome.no_results
            cards_max = max(cards_max, outcome.cards_found)
            raws = outcome.raws
            pass_raws.append(raws)
            parsed = parse_cards(raws, self._base, today, target, search_url=url)
            cards_max = max(cards_max, parsed.cards_found)
            parse_errors += parsed.parse_errors
            for name, n in parsed.field_misses.items():
                field_misses[name] = field_misses.get(name, 0) + n
            if parsed.strategy != "none":
                strategy = parsed.strategy
            for error in parsed.errors:
                log.debug("scraper_parse_error", keyword=keyword, error=error)
            if raws:
                via = Counter(f"{k}={v}" for raw in raws for k, v in (raw.get("via") or {}).items())
                log.debug("scraper_parse_via", keyword=keyword, via=dict(via))
            new_posts = 0
            for post in parsed.posts:
                if post.source_ref not in merged and len(merged) < cap:
                    merged[post.source_ref] = post
                    new_posts += 1
            stop = outcome.stop
            drift = drift_detected(len(parsed.posts), outcome.cards_found, outcome.no_results)
            log.debug(
                "scraper_pass", keyword=keyword, date_filter=date_filter, sort=spec.sort, variant=spec.kind,
                scrolls=outcome.scrolls, cards=outcome.cards_found, posts=len(parsed.posts), new=new_posts,
                stop=outcome.stop,
            )
            if drift:
                any_drift = True
                log.warning(
                    "scraper_layout_drift", keyword=keyword, final_path=urlsplit(page.url).path,
                    cards_found=outcome.cards_found, parse_errors=parsed.parse_errors,
                )
                break  # an unknown layout will not get better with another date filter
        posts = list(merged.values())
        drift = any_drift and not posts
        if drift:
            stop = "drift"
        elif not posts and no_results:
            stop = "no_results"
        elif len(posts) >= cap:
            stop = "cap"
        self._remember_cards(pass_raws)
        log.debug(
            "scraper_search_done", keyword=keyword, strategy=strategy, sdui_screen=self._sdui_screen(page),
            cards_found=cards_max, posts=len(posts), scrolls=scrolls_total, passes=passes, expanded=expanded_total,
            stop_reason=stop, parse_errors=parse_errors, field_misses=field_misses, no_results=no_results,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return KeywordScrapeResult(
            keyword=keyword,
            posts=posts,
            scrolls=scrolls_total,
            parse_errors=parse_errors,
            drift_warning=drift,
            cards_found=cards_max,
            no_results=no_results,
            field_misses=field_misses,
            strategy=strategy,
            passes=passes,
            expanded=expanded_total,
            stop_reason=stop,
        )

    def _scan_pass(
        self, page: Page, keyword: str, date_filter: str, url: str, target: int, expanded_so_far: int, what: str
    ) -> _PassOutcome:
        settings = self._settings
        self._checkpoint(page, what=what)  # before each page load (e.g. li_at gone since the last one)
        self._pacer.delay()
        try:
            response = page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            self._raise_if_latched()
            raise ScrapeError(f"Could not open search results{what}: {exc}") from exc
        self._checkpoint(page, response, what=what)
        try:
            page.wait_for_function(CONTENT_READY_JS, arg=SIGNAL_ARGS, timeout=content_timeout_ms(settings))
        except PlaywrightError:
            pass  # nothing appeared: possible layout drift, decided by the caller
        extras = self._checkpoint(page, what=what)
        cards_found, no_results = extras["cards_found"], extras["no_results"]

        scrolls = idle = expanded = 0
        stop: StopReason = "no_growth"
        if cards_found > 0:
            while True:
                if cards_found >= target:
                    stop = "cap"
                    break
                if scrolls >= settings.scrape_max_scrolls:
                    stop = "max_scrolls"
                    break
                before = cards_found
                self._pacer.delay()
                self._hover_scroll_column(page)
                self._wheel(page, self._pacer.randint(700, 1400))
                scrolls += 1
                grew = self._wait_for_more_cards(page, before)
                extras = self._checkpoint(page, what=what)
                cards_found, no_results = extras["cards_found"], extras["no_results"]
                idle = 0 if (grew and cards_found > before) else idle + 1
                if idle >= settings.scrape_no_growth_scrolls:
                    stop = "cap" if cards_found >= target else "no_growth"
                    break
            budget = max(0, settings.scrape_expand_max_per_keyword - expanded_so_far)
            expanded = self._expand(page, min(target, cards_found), what, budget)
        try:
            raws = extract_raw_cards(page) if cards_found > 0 else []
        except PlaywrightError as exc:
            self._raise_if_latched()
            raise ScrapeError(f"Could not read result cards{what}: {exc}") from exc
        return _PassOutcome(raws, scrolls, expanded, max(cards_found, len(raws)), no_results, stop)

    def _remember_cards(self, pass_raws: list[list[dict[str, Any]]]) -> None:
        """Keep the raw cards of this scan for ``capture_links``: ``card_index`` is trusted only for
        the LAST pass (the page that stays loaded); earlier passes are located by component key."""
        self._card_raws = {}
        last = len(pass_raws) - 1
        for i, raws in enumerate(pass_raws):
            for raw in raws:
                ref = ref_of_raw(raw)
                if ref is None:
                    continue
                copy = dict(raw)
                if i != last:
                    copy.pop("card_index", None)
                self._card_raws[ref] = copy

    def capture_links(self, refs: Sequence[str]) -> LinkCaptureResult:
        """Phase 1.4: permalink capture for ``refs`` (``ScrapedPost.source_ref`` values of the LAST
        ``scan``; its page is still loaded). Opens each card's "…" menu and presses "Copy link to
        post" - nothing else - for at most ``scrape_permalink_max_per_keyword`` cards; 3 consecutive
        misses end it; a card no longer in the DOM is skipped. Raises ``AccountSafetyStop``."""
        page = self._page
        if page is None or not self.is_open:
            raise ScrapeError("LinkedInSearcher is not open (use it as a context manager)")
        cap = self._settings.scrape_permalink_max_per_keyword
        if cap <= 0 or not refs:
            return LinkCaptureResult()
        raws = list(self._card_raws.values())
        chosen = eligible_refs(raws, refs, len(refs))
        if not chosen:
            return LinkCaptureResult()
        targets = [self._card_raws[ref] for ref in chosen]
        what = " while copying post links"
        stats = capture_permalinks(
            page, targets, cap=cap, pacer=self._pacer, checkpoint=lambda: self._checkpoint(page, what=what),
            keyword=self._last_keyword,
        )
        links: dict[str, str] = {}
        for ref, raw in zip(chosen, targets):
            _, url = canonical_post_url(raw.get("copied_url"), self._base)
            if url:
                links[ref] = url
        return LinkCaptureResult(links=links, attempts=stats.attempts, found=len(links))

    def search(self, keyword: str, max_posts: int) -> KeywordScrapeResult:
        """Phase 1.2/1.3 entry point: ``scan`` then ``capture_links`` for every capturable post."""
        result = self.scan(keyword, max_posts)
        if not result.posts or self._settings.scrape_permalink_max_per_keyword <= 0:
            return result
        captured = self.capture_links([p.source_ref for p in result.posts])
        return replace(
            result,
            posts=apply_links(result.posts, captured.links),
            permalink_attempts=captured.attempts,
            permalinks_found=captured.found,
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

    def _expand(self, page: Page, n: int, what: str, budget: int) -> int:
        """Click "see more" on the first ``n`` cards (at most ``budget`` clicks), paced, with a
        checkpoint after each. Returns the number of clicks made."""
        if n <= 0 or budget <= 0:
            return 0
        try:
            buttons = see_more_buttons(page, n)
        except PlaywrightError:
            self._raise_if_latched()
            return 0
        done = 0
        for button in buttons:
            if done >= budget:
                break
            try:
                if not button.is_visible():
                    continue
            except PlaywrightError:
                continue
            self._pacer.short()
            try:
                self._click(button)
                done += 1
            except PlaywrightError:
                pass  # an unexpandable card is parsed as-is
            self._checkpoint(page, what=what)
        return done


@dataclass(frozen=True)
class _PassOutcome:
    raws: list[dict[str, Any]]
    scrolls: int
    expanded: int
    cards_found: int
    no_results: bool
    stop: StopReason


def apply_links(posts: Sequence[ScrapedPost], links: dict[str, str]) -> list[ScrapedPost]:
    """Pure: upgrade posts that got a permalink (url, post link kind, and the URN as source_ref when
    the link carries one). A post whose new ref repeats an earlier one is dropped."""
    out: list[ScrapedPost] = []
    seen: set[str] = set()
    for post in posts:
        url = links.get(post.source_ref)
        if url:
            ref = urn_from_href(url) or post.source_ref
            post = replace(post, source_ref=ref, url=url, link_kind=link_kind_of(url) or "post")
        if post.source_ref in seen:
            continue
        seen.add(post.source_ref)
        out.append(post)
    return out
