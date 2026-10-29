"""Scraper audit fixes (slice B) against the real EXTRACT_JS in headless Chromium, on saved/synthetic
HTML set directly on the page (``set_content``): no navigation, no network, never linkedin.com.

#2 actor block fallback, #7 truncation flag, #12 hrefs of truncated link labels, #13 reshare original.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from sales0.scraper.parsing import parse_cards
from sales0.scraper.search import extract_raw_cards

pytestmark = pytest.mark.browser

PAGES = Path(__file__).resolve().parents[2] / "fixtures" / "linkedin_pages"
SDUI_HTML = (PAGES / "search_results_sdui.html").read_text(encoding="utf-8")
BASE = "https://www.linkedin.com"
TODAY = date(2026, 10, 27)


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, chromium_sandbox=True, args=["--host-resolver-rules=MAP * ~NOTFOUND"])
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context()
    context.route("**/*", lambda route: route.abort())
    p = context.new_page()
    yield p
    context.close()


def _parse(page: Page, html: str):
    page.set_content(html)
    raws = extract_raw_cards(page)
    return raws, parse_cards(raws, BASE, TODAY, cap=15)


def _card(commentary: str, *, nested: str = "", expand: str = "") -> str:
    return (
        '<main><div role="listitem" componentkey="expandedKEYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFeedType_FLAGSHIP_SEARCH">'
        '<div data-view-name="feed-full-update"><div componentkey="KEYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA">'
        '<a data-view-name="feed-actor" href="https://www.linkedin.com/in/ann-bee/"><p>Ann Bee</p></a>'
        "<p>Head of L&amp;D at Orvane</p><p>3h</p>"
        f'<p data-view-name="feed-commentary"><span data-testid="expandable-text-box">{commentary}{expand}</span></p>'
        f"{nested}</div></div></div></main>"
    )


# --------------------------------------------------------------------------- #2
def test_fixture_with_feed_actor_image_renamed_still_gets_time_and_headline_on_all_posts(page: Page) -> None:
    html = SDUI_HTML.replace('data-view-name="feed-actor-image"', 'data-view-name="feed-actor"')
    assert html != SDUI_HTML
    _, parsed = _parse(page, html)
    assert len(parsed.posts) == 6
    assert all(p.posted_relative for p in parsed.posts), [p.posted_relative for p in parsed.posts]
    _, original = _parse(page, SDUI_HTML)  # the unmodified fixture: headline is genuinely absent on one post
    assert [p.author_headline for p in parsed.posts] == [p.author_headline for p in original.posts]
    assert sum(1 for p in parsed.posts if p.author_headline) == 5
    assert "posted_relative" not in parsed.field_misses
    assert parsed.field_misses.get("author_headline") == 1


# --------------------------------------------------------------------------- #7
def test_card_with_an_unexpanded_see_more_toggle_is_flagged_truncated(page: Page) -> None:
    button = '<button data-testid="expandable-text-button" type="button"><span>…</span><span> more</span></button>'
    raws, parsed = _parse(page, _card("We need a trainer for ten engineers in Pune next month ", expand=button))
    assert raws[0]["truncated"] is True
    assert parsed.posts[0].truncated is True
    assert parsed.field_misses.get("truncated") == 1
    assert parsed.posts[0].text == "We need a trainer for ten engineers in Pune next month"


def test_fully_expanded_card_is_not_truncated(page: Page) -> None:
    raws, parsed = _parse(page, _card("We need a trainer for ten engineers in Pune next month."))
    assert raws[0]["truncated"] is False
    assert parsed.posts[0].truncated is False
    assert "truncated" not in parsed.field_misses


def test_hidden_see_more_toggle_does_not_count_as_truncated(page: Page) -> None:
    button = '<button data-testid="expandable-text-button" style="display:none"><span>…more</span></button>'
    raws, _ = _parse(page, _card("A complete short post about AI training.", expand=button))
    assert raws[0]["truncated"] is False


def test_trailing_ellipsis_marker_without_a_button_counts_as_truncated(page: Page) -> None:
    raws, parsed = _parse(page, _card("We need a trainer for ten engineers in Pune next month …"))
    assert raws[0]["truncated"] is True and parsed.posts[0].truncated is True


# --------------------------------------------------------------------------- #12
def test_href_of_a_truncated_link_label_is_appended_but_hashtag_and_mention_links_are_not(page: Page) -> None:
    links = (
        ' <a href="https://lnkd.in/dAbC123x"><strong>learning.example/br…</strong></a>'
        ' <a href="https://www.linkedin.com/search/results/all/?keywords=%23aiworkshop"><strong>#AIWorkshop</strong></a>'
        ' <a href="https://www.linkedin.com/in/someone-else/">@Someone</a>'
        ' <a href="https://visible.example/page">https://visible.example/page</a>'
    )
    _, parsed = _parse(page, _card("Brief here:" + links))
    text = parsed.posts[0].text
    assert "https://lnkd.in/dAbC123x" in text
    assert "linkedin.com/search" not in text and "someone-else" not in text
    assert text.count("visible.example/page") == 1  # already visible: not duplicated


# --------------------------------------------------------------------------- #13
RESHARE_NESTED = (
    '<div data-view-name="feed-full-update"><a data-view-name="feed-actor" href="https://www.linkedin.com/in/orig-p/">'
    '<p>Orig Poster</p></a><p data-view-name="feed-commentary"><span data-testid="expandable-text-box">'
    "Original: hiring a GenAI trainer in Mumbai, mail hr@orig.example</span></p></div>"
)


def test_reshare_with_own_commentary_keeps_the_original_text_marked(page: Page) -> None:
    _, parsed = _parse(page, _card("Great opportunity, please apply!", nested=RESHARE_NESTED))
    text = parsed.posts[0].text
    assert text.startswith("Great opportunity, please apply!")
    assert "\n\n[Reshared post] Original: hiring a GenAI trainer in Mumbai, mail hr@orig.example" in text


def test_reshare_without_commentary_still_uses_the_original_alone(page: Page) -> None:
    html = _card("", nested=RESHARE_NESTED)
    _, parsed = _parse(page, html)
    assert parsed.posts[0].text.startswith("Original: hiring a GenAI trainer")
