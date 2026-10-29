"""Inbox feedback A2: a post permalink read off what the card capture already holds (no extra LinkedIn navigation).

Link order: post permalink -> author profile -> (only if neither) the search page. Synthetic card data only."""

from __future__ import annotations

from datetime import date

from sales0.scraper.parsing import card_post_urn, post_from_raw

BASE = "https://www.linkedin.com"
SEARCH = f"{BASE}/search/results/content/?keywords=ai%20workshop"
TODAY = date(2026, 10, 29)
A1 = "urn:li:activity:7300000000000000011"


def raw(card: dict | None = None, *, text: str = "Need a GenAI workshop for 40 managers in Pune", profile: str | None = "/in/asha-fx/"):  # type: ignore[no-untyped-def]
    out = {"strategy": "sdui", "component_key": "KEY1", "text": text, "author_name": "Asha", "author_profile_url": profile}
    if card is not None:
        out["card"] = card
    return out


def test_one_activity_urn_in_the_card_html_becomes_the_post_link() -> None:
    r = raw({"outer_html": f'<div data-urn="{A1}"><a href="/in/asha-fx/">Asha</a></div>', "links": ["/in/asha-fx/"]})
    assert card_post_urn(r, BASE) == A1
    post = post_from_raw(r, BASE, TODAY, search_url=SEARCH)
    assert (post.url, post.link_kind, post.source_ref) == (f"{BASE}/feed/update/{A1}/", "post", "ck:KEY1")


def test_a_same_site_post_link_counts_and_ugcpost_alias_of_the_same_post_is_ignored() -> None:
    r = raw({"links": ["/posts/asha-fx_genai-activity-7300000000000000011-AbCd/"],
             "outer_html": '<div data-x="urn:li:ugcPost:7300000000000000999"></div>'})
    assert card_post_urn(r, BASE) == A1  # one activity id wins over its ugcPost alias


def test_ambiguous_foreign_or_reshared_cards_fall_back_to_the_profile() -> None:
    two = raw({"outer_html": f"{A1} urn:li:activity:7300000000000000022"})
    foreign = raw({"links": ["https://elsewhere.example/feed/update/urn:li:activity:7300000000000000033/"]})
    reshare = raw({"outer_html": A1}, text="Great point!\n\n[Reshared post] Need a GenAI workshop")
    for r in (two, foreign, reshare):
        assert card_post_urn(r, BASE) is None
        post = post_from_raw(r, BASE, TODAY, search_url=SEARCH)
        assert (post.url, post.link_kind) == (f"{BASE}/in/asha-fx/", "profile")


def test_search_page_only_when_there_is_neither_a_post_nor_a_profile() -> None:
    post = post_from_raw(raw({}, profile=None), BASE, TODAY, search_url=SEARCH)
    assert (post.url, post.link_kind) == (SEARCH, "search")
    assert card_post_urn(raw(None), BASE) is None  # no card capture at all
