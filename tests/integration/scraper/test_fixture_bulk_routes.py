"""Fixture LinkedIn bulk results: pages, the infinite-scroll fragment route and the markup contract
(in-process TestClient; the browser behaviour is covered by test_scan_bulk.py)."""

from __future__ import annotations

import re
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from sales0.linkedin_fixture import build_app
from sales0.linkedin_fixture.bulk import PAGE_SIZE, results_for, variant_for
from sales0.scraper.urls import FIXTURE_PREFIX
from sales0.services.keyword_library import SUGGESTED_KEYWORDS

SEARCH = f"{FIXTURE_PREFIX}/search/results/content/"
MORE = f"{SEARCH}more/"
KEYWORDS = {v: next(k.text for k in SUGGESTED_KEYWORDS if results_for(k.text) is not None and variant_for(k.text) == v) for v in ("sdui", "listitem")}


@pytest.fixture
def client() -> TestClient:
    c = TestClient(build_app(), follow_redirects=False)
    c.cookies.set("li_at", "fixture-session")
    return c


def _q(kw: str, **extra: str) -> str:
    return "?" + urlencode({"keywords": kw, "datePosted": '"past-month"', "sortBy": '"date_posted"', **extra})


@pytest.mark.parametrize("variant", ["sdui", "listitem"])
def test_the_first_page_has_ten_cards_and_a_sentinel(client: TestClient, variant: str) -> None:
    html = client.get(SEARCH + _q(KEYWORDS[variant])).text
    assert html.count('<div role="listitem"') == PAGE_SIZE
    assert 'id="fx-sentinel"' in html and "__fixture_query" in html
    assert ("data-view-name=\"feed-full-update\"" in html) is (variant == "sdui")
    assert 'aria-label="Open control menu for post by Bulk Author' in html


@pytest.mark.parametrize("variant", ["sdui", "listitem"])
def test_the_markup_has_no_urn_or_post_links_and_no_legacy_classes(client: TestClient, variant: str) -> None:
    kw = KEYWORDS[variant]
    for url in (SEARCH + _q(kw), MORE + _q(kw, start="10")):
        html = client.get(url).text
        assert "urn:li:" not in html and "/feed/update/" not in html and "/posts/" not in html.replace('"posts"', "")
        assert "global-nav" not in html and "update-components" not in html
        assert not re.search(r'href="[^"]*/posts/', html)


def test_the_listitem_variant_is_marker_free(client: TestClient) -> None:
    html = client.get(SEARCH + _q(KEYWORDS["listitem"])).text
    assert "data-view-name" not in html.split("<script>")[0]
    assert 'componentkey="update-card-focus' in html and 'data-testid="expandable-text-box"' in html


def test_more_pages_walk_the_list_then_end_empty(client: TestClient) -> None:
    kw = KEYWORDS["sdui"]
    counts = [client.get(MORE + _q(kw, start=str(s))).text.count('role="listitem"') for s in range(0, 100, 10)]
    assert counts == [10] * 9 + [0]
    assert client.get(MORE + _q(kw, start="500")).text.strip() == ""


def test_fragments_honour_date_posted_and_sort(client: TestClient) -> None:
    kw = KEYWORDS["sdui"]
    week = urlencode({"keywords": kw, "datePosted": '"past-week"', "sortBy": '"date_posted"'})
    pages = [client.get(f"{MORE}?{week}&start={s}").text.count('role="listitem"') for s in (0, 10, 20, 30)]
    assert pages == [10, 10, 4, 0]
    key_of = lambda html: re.findall(r'<div componentkey="(Bk[^"]+)"', html)  # noqa: E731
    latest = key_of(client.get(f"{MORE}?{week}&start=0").text)
    relevance = key_of(client.get(f"{MORE}?" + urlencode({"keywords": kw, "datePosted": '"past-week"', "sortBy": '"relevance"'}) + "&start=0").text)
    assert latest != relevance and len(latest) == len(relevance) == 10


def test_the_pages_need_a_session(client: TestClient) -> None:
    anonymous = TestClient(build_app(), follow_redirects=False)
    assert anonymous.get(SEARCH + _q(KEYWORDS["sdui"])).status_code == 302
    assert anonymous.get(MORE + _q(KEYWORDS["sdui"], start="0")).status_code == 401


def test_legacy_keywords_keep_their_phase_1_2_pages(client: TestClient) -> None:
    html = client.get(SEARCH + "?keywords=ai%20workshop").text
    assert html.count('data-view-name="feed-full-update"') == 4 and "fx-sentinel" not in html
