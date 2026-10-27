"""The selector contract against LinkedIn-shaped markup the fixture does not serve
(real global-nav header, ugcPost/share URNs, a repost nesting its original)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, sync_playwright

from sales0.config import Settings
from sales0.scraper.detection import read_signals
from sales0.scraper.search import LinkedInSearcher

pytestmark = pytest.mark.browser

LIVE = "https://www.linkedin.com"


@pytest.fixture(scope="module")
def page() -> Iterator[Page]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            yield browser.new_page()
        finally:
            browser.close()


def _card(urn: str, text: str, inner: str = "") -> str:
    return (
        f'<div class="feed-shared-update-v2" data-urn="{urn}">'
        '<a class="update-components-actor__meta-link" href="/in/someone/">'
        '<span class="update-components-actor__title"><span aria-hidden="true">Some One</span></span></a>'
        f'<div class="update-components-text">{text}</div>{inner}</div>'
    )


@pytest.mark.parametrize(
    "nav",
    [
        '<header id="global-nav" class="global-nav global-nav--a11y"><div class="global-nav__content">'
        '<nav class="global-nav__nav" aria-label="Primary Navigation"></nav></div></header>',
        '<header class="global-nav"></header>',
        '<div id="global-nav"></div>',
        '<nav class="global-nav" aria-label="Primary"></nav>',  # the fixture's markup
    ],
)
def test_logged_in_marker_matches_real_and_fixture_nav(page: Page, nav: str) -> None:
    page.set_content(f"<body>{nav}<main></main></body>")
    assert read_signals(page, None).has_logged_in_marker is True


def test_logged_in_marker_is_absent_on_a_signed_out_page(page: Page) -> None:
    page.set_content('<body><header class="nav"><a href="/login">Sign in</a></header></body>')
    assert read_signals(page, None).has_logged_in_marker is False


def test_ugcpost_and_share_cards_parse_and_nested_or_repeated_cards_dedupe(page: Page) -> None:
    page.set_content(
        "<body><ul>"
        + _card("urn:li:ugcPost:7311", "A ugcPost card")
        + _card("urn:li:share:7322", "A share card")
        + _card("urn:li:activity:7333", "A repost", inner=_card("urn:li:share:7344", "The reshared original"))  # nested: one post
        + _card("urn:li:ugcPost:7311", "A ugcPost card")  # LinkedIn re-rendered the same card
        + _card("urn:li:group:1", "Not a post")
        + "</ul></body>"
    )
    searcher = LinkedInSearcher(Settings(linkedin_mode="live"))
    posts, errors = searcher._parse(page, 15)
    assert [p.urn for p in posts] == ["urn:li:ugcPost:7311", "urn:li:share:7322", "urn:li:activity:7333"]
    assert posts[0].url == f"{LIVE}/feed/update/urn:li:ugcPost:7311/"
    assert posts[0].author_profile_url == f"{LIVE}/in/someone/"
    assert errors == 0  # the urn:li:group card never matches RESULT_CARD at all
