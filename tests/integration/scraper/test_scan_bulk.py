"""Phase 1.4 slice C: ``scan`` / ``capture_links`` against the fixture's bulk corpus (real headless
Chromium, infinite scroll, both markup variants). Only the fixture is ever contacted."""

from __future__ import annotations

import re

import pytest

from sales0.config import Settings, reset_settings
from sales0.linkedin_fixture.bulk import expected_verdict, results_for, variant_for
from sales0.scraper.search import LinkedInSearcher, apply_links
from sales0.scraper.urls import base_url, urn_from_href
from sales0.services.keyword_library import SUGGESTED_KEYWORDS

pytestmark = pytest.mark.browser


def _keyword(variant: str) -> str:
    return next(k.text for k in SUGGESTED_KEYWORDS if results_for(k.text) is not None and variant_for(k.text) == variant)


SDUI_KW = _keyword("sdui")
LISTITEM_KW = _keyword("listitem")


def _depth(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    """The Phase 1.4 defaults (two date passes, 2 idle scrolls) unless overridden."""
    monkeypatch.setenv("SALES0_SCRAPE_DATE_FILTERS", env.pop("filters", "past-week,past-month"))
    monkeypatch.setenv("SALES0_SCRAPE_NO_GROWTH_SCROLLS", env.pop("no_growth", "2"))
    for key, value in env.items():
        monkeypatch.setenv(f"SALES0_{key.upper()}", value)
    reset_settings()


def _fixture_state(searcher: LinkedInSearcher) -> dict:
    page = searcher._page
    assert page is not None
    return page.evaluate(
        "() => ({q: window.__fixture_query, opens: window.__fixture_menu_opens, clicks: window.__fixture_menu_clicks,"
        " loads: window.__fixture_loads})"
    )


@pytest.mark.parametrize("variant,keyword", [("sdui", SDUI_KW), ("listitem", LISTITEM_KW)])
def test_scan_reads_80_distinct_posts_across_two_passes_without_opening_a_menu(
    connected_settings: Settings, monkeypatch: pytest.MonkeyPatch, variant: str, keyword: str
) -> None:
    _depth(monkeypatch)
    with LinkedInSearcher(connected_settings.__class__()) as searcher:  # fresh Settings from the env
        result = searcher.scan(keyword, 80)
        state = _fixture_state(searcher)
    assert result.strategy == variant
    assert len(result.posts) == 80 and len({p.source_ref for p in result.posts}) == 80
    assert result.stop_reason == "cap" and result.passes == 2 and result.parse_errors == 0
    assert result.drift_warning is False and result.scrolls >= 8
    assert state["opens"] == 0 and state["clicks"] == [], "scan must never open a post menu"
    assert state["q"]["sortBy"] == '"date_posted"' and state["q"]["datePosted"] == '"past-month"'
    assert state["loads"] >= 7, "infinite scroll must have fetched further pages"
    # newest first; every post is one of the bulk posts and carries its author, headline and time
    assert [p.posted_date for p in result.posts] == sorted((p.posted_date for p in result.posts), reverse=True)
    assert all(p.author_name and p.author_headline and p.author_profile_url and p.posted_relative for p in result.posts)
    assert all(expected_verdict(p.text) is not None for p in result.posts)
    assert result.expanded > 0 and result.expanded <= connected_settings.scrape_expand_max_per_keyword


def test_buyer_posts_are_read_in_full_including_the_see_more_tail(connected_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _depth(monkeypatch, filters="past-week", scrape_expand_max_per_keyword="200")
    for keyword in (SDUI_KW, LISTITEM_KW):
        with LinkedInSearcher(connected_settings.__class__()) as searcher:
            result = searcher.scan(keyword, 24)
        buyers = [p for p in result.posts if expected_verdict(p.text) == "genuine"]
        assert buyers, keyword
        for post in buyers:
            assert re.search(r"contact\d{3}@[\w-]+\.example", post.text) and "+91 90000 0" in post.text
            assert post.text.rstrip().endswith(")") and "more" not in post.text.split()[-1].lower()


def test_date_filters_and_sort_are_sent_and_the_scan_stops_at_the_end_of_the_list(
    connected_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _depth(monkeypatch, filters="past-week", scrape_sort="relevance")
    with LinkedInSearcher(connected_settings.__class__()) as searcher:
        result = searcher.scan(SDUI_KW, 80)
        state = _fixture_state(searcher)
    assert state["q"]["sortBy"] == '"relevance"' and state["q"]["datePosted"] == '"past-week"'
    assert len(result.posts) == len(results_for(SDUI_KW, "past-week")) == 24
    assert result.passes == 1 and result.stop_reason == "no_growth"


def test_max_scrolls_ends_a_pass_with_the_max_scrolls_reason(connected_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _depth(monkeypatch, filters="past-month", scrape_max_scrolls="2", scrape_max_posts_per_keyword="90")
    with LinkedInSearcher(connected_settings.__class__()) as searcher:
        result = searcher.scan(LISTITEM_KW, 90)
    assert result.scrolls == 2 and result.stop_reason == "max_scrolls" and 20 <= len(result.posts) < 90


def test_the_expand_budget_is_respected(connected_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _depth(monkeypatch, filters="past-week", scrape_expand_max_per_keyword="3")
    with LinkedInSearcher(connected_settings.__class__()) as searcher:
        result = searcher.scan(SDUI_KW, 24)
    assert result.expanded == 3
    _depth(monkeypatch, filters="past-week", scrape_expand_max_per_keyword="0")
    with LinkedInSearcher(connected_settings.__class__()) as searcher:
        assert searcher.scan(SDUI_KW, 24).expanded == 0


@pytest.mark.parametrize("keyword", [SDUI_KW, LISTITEM_KW])
def test_capture_links_copies_post_links_on_both_tiers_and_never_three_misses_in_a_row(
    connected_settings: Settings, monkeypatch: pytest.MonkeyPatch, keyword: str
) -> None:
    _depth(monkeypatch, filters="past-week", scrape_permalink_max_per_keyword="15")
    settings = connected_settings.__class__()
    with LinkedInSearcher(settings) as searcher:
        scanned = searcher.scan(keyword, 24)
        refs = [p.source_ref for p in scanned.posts]
        captured = searcher.capture_links(refs)
        state = _fixture_state(searcher)
    base = base_url(settings)
    assert captured.attempts == 15 and 11 <= captured.found <= 13  # every 5th post's menu copies nothing
    assert len(captured.links) == captured.found
    assert set(captured.links) <= set(refs)
    for ref, url in captured.links.items():
        assert url.startswith(f"{base}/feed/update/urn:li:activity:") and "?" not in url
        assert urn_from_href(url)
    assert set(state["clicks"]) == {"Copy link to post"}, "only 'Copy link to post' may ever be clicked"
    assert state["opens"] == captured.attempts
    upgraded = apply_links(scanned.posts, captured.links)
    assert sum(1 for p in upgraded if p.link_kind == "post") == captured.found
    assert all(p.urn for p in upgraded if p.link_kind == "post")


def test_capture_links_skips_cards_no_longer_in_the_dom(connected_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _depth(monkeypatch, filters="past-week")
    with LinkedInSearcher(connected_settings.__class__()) as searcher:
        scanned = searcher.scan(LISTITEM_KW, 12)
        refs = [p.source_ref for p in scanned.posts][:6]
        searcher._page.evaluate(  # type: ignore[union-attr]
            "() => document.querySelectorAll('[role=\"listitem\"]').forEach((e, i) => { if (i < 3) e.remove(); })"
        )
        captured = searcher.capture_links(refs)
        state = _fixture_state(searcher)
    assert captured.attempts == 3 and state["opens"] == 3  # the 3 removed cards cost nothing


def test_capture_links_honours_a_cap_of_zero_and_unknown_refs(connected_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _depth(monkeypatch, filters="past-week", scrape_permalink_max_per_keyword="0")
    with LinkedInSearcher(connected_settings.__class__()) as searcher:
        scanned = searcher.scan(SDUI_KW, 6)
        assert searcher.capture_links([p.source_ref for p in scanned.posts]).attempts == 0
        assert _fixture_state(searcher)["opens"] == 0
    _depth(monkeypatch, filters="past-week")
    with LinkedInSearcher(connected_settings.__class__()) as searcher:
        searcher.scan(SDUI_KW, 6)
        assert searcher.capture_links(["ck:not-a-real-ref"]).attempts == 0


def test_search_still_scans_then_captures_for_old_callers(connected_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _depth(monkeypatch, filters="past-week", scrape_permalink_max_per_keyword="6")
    with LinkedInSearcher(connected_settings.__class__()) as searcher:
        result = searcher.search(LISTITEM_KW, 12)
    assert len(result.posts) == 12 and result.permalink_attempts == 6
    assert result.permalinks_found == sum(1 for p in result.posts if p.link_kind == "post") >= 4
    assert result.passes == 1
