"""Permalink capture ("Copy link to post") on real headless Chromium against the SDUI fixture.

Covers: clipboard isolation (the page's original clipboard is never called), the per-keyword
cap (0 / 1 / default), the 3-consecutive-miss stop, that only "Copy link to post" is ever
clicked, and a safety checkpoint after every menu action. A real LinkedIn account is never
touched: the fixture server is local and the profile lives in this test's tmp data dir.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Page

from sales0.config import Settings, get_settings, reset_settings
from sales0.linkedin_fixture.corpus import PERMALINK_MISS_KEYWORD, permalink_miss_posts, posts_for
from sales0.scraper import permalink as permalink_module
from sales0.scraper.browser import first_page, open_context
from sales0.scraper.pacing import Pacer
from sales0.scraper.search import LinkedInSearcher
from sales0.scraper.urls import base_url, permalink

pytestmark = pytest.mark.browser

# Stands in for the browser's own clipboard: records every call, never touches the system.
ORIGINAL_CLIPBOARD_SPY = r"""
window.__original_clipboard_calls = [];
try {
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: {
      writeText: (t) => { window.__original_clipboard_calls.push(["writeText", String(t)]); return Promise.resolve(); },
      write: () => { window.__original_clipboard_calls.push(["write"]); return Promise.resolve(); },
      readText: () => { window.__original_clipboard_calls.push(["readText"]); return Promise.resolve(""); },
    },
  });
} catch (e) {}
"""

MENU_STATE_JS = """() => ({
  opens: window.__fixture_menu_opens || 0,
  clicks: (window.__fixture_menu_clicks || []).slice(),
  hooked: Array.isArray(window.__sales0_copied),
  original: (window.__original_clipboard_calls || []).slice(),
})"""


def _with_cap(monkeypatch: pytest.MonkeyPatch, cap: int | None) -> Settings:
    if cap is not None:
        monkeypatch.setenv("SALES0_SCRAPE_PERMALINK_MAX_PER_KEYWORD", str(cap))
    reset_settings()
    return get_settings()


def _search(settings: Settings, keyword: str, *, spy: bool = False) -> tuple[Any, dict[str, Any]]:
    with LinkedInSearcher(settings) as searcher:
        if spy:
            searcher._context.add_init_script(ORIGINAL_CLIPBOARD_SPY)  # type: ignore[union-attr]
        result = searcher.search(keyword, 15)
        state = searcher._page.evaluate(MENU_STATE_JS)  # type: ignore[union-attr]
    return result, state


def _profile(base: str, post: Any) -> str:
    return f"{base}/{post.profile_path}/"


def test_clipboard_hook_captures_writes_without_calling_the_original_even_when_clipboard_is_undefined(
    connected_settings: Settings,
) -> None:
    base = base_url(connected_settings)
    with open_context(connected_settings, headless=True) as context:
        context.add_init_script(ORIGINAL_CLIPBOARD_SPY)
        page: Page = first_page(context)
        page.goto(f"{base}/feed/", wait_until="domcontentloaded")
        page.evaluate(permalink_module.CLIPBOARD_HOOK_JS)
        page.evaluate(permalink_module.CLIPBOARD_HOOK_JS)  # idempotent
        page.evaluate("() => navigator.clipboard.writeText('https://example.invalid/a')")
        page.evaluate("() => navigator.clipboard.write([])")
        assert page.evaluate("() => window.__original_clipboard_calls") == []
        assert page.evaluate(permalink_module.TAKE_COPIED_JS) == ["https://example.invalid/a"]
        assert page.evaluate(permalink_module.TAKE_COPIED_JS) == []  # take clears

        # A page with no navigator.clipboard at all.
        page.evaluate("() => Object.defineProperty(navigator, 'clipboard', {configurable: true, value: undefined})")
        page.evaluate("() => { window.__sales0_clipboard_hooked = false; }")
        page.evaluate(permalink_module.CLIPBOARD_HOOK_JS)
        page.evaluate("() => navigator.clipboard.writeText(42)")
        assert page.evaluate(permalink_module.TAKE_COPIED_JS) == ["42"]


def test_default_cap_links_p5_to_its_clean_permalink_and_only_copy_link_is_clicked(
    connected_settings: Settings,
) -> None:
    base = base_url(connected_settings)
    result, state = _search(connected_settings, "ai workshop", spy=True)
    corpus = {p.id: p for p in posts_for("ai workshop")}  # P1 noop, P5 urn, P6 noop, P7 none

    assert result.strategy == "sdui"
    assert (result.permalink_attempts, result.permalinks_found) == (3, 1)
    assert len(result.posts) == 4 and result.drift_warning is False and result.parse_errors == 0
    by_ref = {p.source_ref: p for p in result.posts}

    p5 = by_ref[corpus["P5"].urn]
    assert p5.urn == corpus["P5"].urn and p5.link_kind == "post"
    assert p5.url == permalink(base, corpus["P5"].urn)
    assert "utm_" not in p5.url and "rcm" not in p5.url and "?" not in p5.url
    for pid in ("P1", "P6", "P7"):
        post = next(p for p in result.posts if p.source_ref == f"ck:{corpus[pid].key_for('ai workshop')}")
        assert post.urn is None and post.link_kind == "profile"
        assert post.url == _profile(base, corpus[pid]) == post.author_profile_url

    assert state["opens"] == 3  # P7 has no menu
    assert state["clicks"] and set(state["clicks"]) == {"Copy link to post"}
    assert state["original"] == [], "the page's own clipboard must never be called"
    assert state["hooked"] is True


def test_cap_zero_skips_capture_entirely_and_cards_link_to_the_author_profile(
    connected_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _with_cap(monkeypatch, 0)
    base = base_url(settings)
    result, state = _search(settings, "ai workshop")
    assert (state["opens"], state["clicks"], state["hooked"]) == (0, [], False)
    assert (result.permalink_attempts, result.permalinks_found) == (0, 0)
    assert len(result.posts) == 4  # counts unchanged
    corpus = posts_for("ai workshop")
    assert [p.source_ref for p in result.posts] == [f"ck:{c.key_for('ai workshop')}" for c in corpus]
    assert all(p.link_kind == "profile" for p in result.posts)
    assert [p.url for p in result.posts] == [_profile(base, c) for c in corpus]


def test_cap_one_opens_exactly_one_menu(connected_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _with_cap(monkeypatch, 1)
    result, state = _search(settings, "ai workshop")
    assert state["opens"] == 1 and state["clicks"] == ["Copy link to post"]
    assert (result.permalink_attempts, result.permalinks_found) == (1, 0)  # P1's menu writes nothing
    assert all(p.link_kind == "profile" for p in result.posts) and len(result.posts) == 4


def test_three_consecutive_misses_end_capture_for_the_keyword(connected_settings: Settings) -> None:
    result, state = _search(connected_settings, PERMALINK_MISS_KEYWORD)
    expected = permalink_miss_posts()
    assert state["opens"] == 3  # the 4th post (P2) is never tried
    assert set(state["clicks"]) == {"Copy link to post"}
    assert (result.permalink_attempts, result.permalinks_found) == (3, 0)
    assert len(result.posts) == 4
    assert [p.source_ref for p in result.posts] == [f"ck:{p.key_for(PERMALINK_MISS_KEYWORD)}" for p in expected]
    assert all(p.source_ref.startswith("ck:") and p.link_kind == "profile" for p in result.posts)


def test_the_capture_runs_once_the_next_keyword_finds_a_fresh_page(connected_settings: Settings) -> None:
    """Two keywords in one session: the hook is installed per results page, counts per keyword."""
    with LinkedInSearcher(connected_settings) as searcher:
        corp = searcher.search("corporate ai training", 15)  # P1 noop, P2 urn, P3 none
        cons = searcher.search("ai consulting", 15)  # P8 urn, P9 noop
    assert (corp.permalink_attempts, corp.permalinks_found) == (2, 1)
    assert (cons.permalink_attempts, cons.permalinks_found) == (2, 1)
    p2 = next(p for p in posts_for("corporate ai training") if p.id == "P2")
    p8 = next(p for p in posts_for("ai consulting") if p.id == "P8")
    assert p2.urn in {p.source_ref for p in corp.posts}
    assert p8.urn in {p.source_ref for p in cons.posts}


# ----------------------------------------------------------------------- order of actions
class EventPacer(Pacer):
    def __init__(self, settings: Settings, events: list[str]) -> None:
        threshold = settings.scrape_delay_min_s
        super().__init__(
            settings.scrape_delay_min_s,
            settings.scrape_delay_max_s,
            random.Random(9),
            lambda s: events.append("delay" if s >= threshold else "short"),
        )


class CheckpointSearcher(LinkedInSearcher):
    def __init__(self, settings: Settings, events: list[str]) -> None:
        self.events = events
        super().__init__(settings, EventPacer(settings, events))

    def _checkpoint(self, page, response=None, *, what=""):  # type: ignore[no-untyped-def]
        self.events.append("checkpoint")
        return super()._checkpoint(page, response, what=what)


@pytest.fixture
def menu_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    events: list[str] = []
    real_click, real_escape = permalink_module._click, permalink_module._press_escape

    def click(element):  # type: ignore[no-untyped-def]
        text = (element.inner_text() or "").strip().lower()
        events.append("copy" if text == "copy link to post" else "menu")
        real_click(element)

    def escape(page):  # type: ignore[no-untyped-def]
        events.append("escape")
        real_escape(page)

    monkeypatch.setattr(permalink_module, "_click", click)
    monkeypatch.setattr(permalink_module, "_press_escape", escape)
    yield events


def test_every_menu_action_is_paced_and_followed_by_a_safety_checkpoint(
    connected_settings: Settings, menu_events: list[str]
) -> None:
    with CheckpointSearcher(connected_settings, menu_events) as searcher:
        result = searcher.search("ai workshop", 15)
    assert result.permalink_attempts == 3
    events = menu_events
    actions = [i for i, e in enumerate(events) if e in ("menu", "copy", "escape")]
    assert [events[i] for i in actions] == ["menu", "copy", "escape"] * 3
    for i in actions:
        assert events[i + 1] == "checkpoint", f"{events[i]} at {i} not followed by a checkpoint: {events}"
        expected_pause = "delay" if events[i] == "menu" else "short"
        assert events[i - 1] == expected_pause, f"{events[i]} at {i} not preceded by a {expected_pause}: {events}"
