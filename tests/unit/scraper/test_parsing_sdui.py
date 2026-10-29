"""Phase 1.2: pure ``post_from_raw`` / ``parse_cards`` over SDUI raw dicts (architecture.md →
"## LinkedIn SDUI search results" → SDUI field extraction): source refs without URNs, the
card link and its kind, text-less containers, in-page dedupe, and the selector contract."""

from __future__ import annotations

import re
from datetime import date

import pytest

from sales0.scraper import selectors as S
from sales0.scraper.parsing import (
    ParseError,
    TextMissing,
    component_key_of,
    parse_cards,
    post_from_raw,
    source_ref_of,
)
from sales0.scraper.urls import link_kind_of

LIVE = "https://www.linkedin.com"
FIX = "http://127.0.0.1:43123/__fixtures/linkedin"
TODAY = date(2026, 10, 27)
SEARCH = f"{LIVE}/search/results/content/?keywords=ai%20workshop&datePosted=%22past-week%22"
KEY = "sxQ3v9LmP1aZ0kQ_7HbN2cRw4tYu8iOpLkJhGfDsAzX"  # synthetic, 43 chars like the capture's


def _sdui(**over: object) -> dict:
    raw: dict = {
        "strategy": "sdui",
        "urn": None,
        "component_key": KEY,
        "post_href": None,
        "author_name": "Arjun Rao",
        "author_profile_url": f"{LIVE}/in/arjun-rao-zx/?miniProfileUrn=x",
        "author_headline": "CTO at Zentrix Retail",
        "posted_relative": "24m • Edited",
        "text": "We want a hands-on AI agents workshop for 30 engineers in Bengaluru … more",
        "has_control_menu": True,
        "via": {"strategy": "sdui"},
    }
    raw.update(over)
    return raw


# --------------------------------------------------------------------------- source ref
def test_a_post_without_any_urn_is_identified_by_its_component_key_and_links_to_the_author() -> None:
    post = post_from_raw(_sdui(), LIVE, TODAY, search_url=SEARCH)
    assert post.source_ref == f"ck:{KEY}" and post.urn is None
    assert post.url == f"{LIVE}/in/arjun-rao-zx/" and post.link_kind == "profile"
    assert post.author_profile_url == f"{LIVE}/in/arjun-rao-zx/"
    assert post.search_url == SEARCH
    assert (post.posted_relative, post.posted_date) == ("24m", TODAY)
    assert post.text == "We want a hands-on AI agents workshop for 30 engineers in Bengaluru"  # inline "… more" dropped


@pytest.mark.parametrize(
    ("over", "ref", "url"),
    [
        # 1. a URN in the post wins over the key
        ({"urn": " urn:li:activity:7300000000000000005 "}, "urn:li:activity:7300000000000000005",
         f"{LIVE}/feed/update/urn:li:activity:7300000000000000005/"),
        # 2. else the post link's URN
        ({"post_href": f"{LIVE}/posts/a_b-ugcPost-7344-x9Zq/"}, "urn:li:ugcPost:7344", f"{LIVE}/feed/update/urn:li:ugcPost:7344/"),
        # 3. else the copied "Copy link to post" URL's URN (tracking query never stored)
        ({"copied_url": f"{LIVE}/posts/arjun_x-activity-7300000000000000005-AbCd?utm_source=share&rcm=ACoAAX"},
         "urn:li:activity:7300000000000000005", f"{LIVE}/feed/update/urn:li:activity:7300000000000000005/"),
        # a foreign URN form is not a URN: the key is the ref
        ({"urn": "urn:li:fsd_profile:ACoAAX"}, f"ck:{KEY}", f"{LIVE}/in/arjun-rao-zx/"),
    ],
)
def test_source_ref_order_is_urn_then_post_link_then_copied_link_then_key(over: dict, ref: str, url: str) -> None:
    post = post_from_raw(_sdui(**over), LIVE, TODAY, search_url=SEARCH)
    assert (post.source_ref, post.url) == (ref, url)
    assert post.link_kind == link_kind_of(post.url)


def test_a_copied_post_link_without_a_urn_keeps_the_key_ref_but_links_to_the_clean_post_url() -> None:
    post = post_from_raw(_sdui(copied_url=f"{FIX}/posts/someone_short-slug?utm_source=share#x"), FIX, TODAY, search_url=SEARCH)
    assert post.source_ref == f"ck:{KEY}"
    assert (post.url, post.link_kind) == (f"{FIX}/posts/someone_short-slug", "post")


def test_a_foreign_or_junk_copied_link_is_ignored_and_the_card_falls_back_to_the_author() -> None:
    for copied in ("https://evil.example/posts/x-activity-1-a", "Link copied", "", None):
        post = post_from_raw(_sdui(copied_url=copied), LIVE, TODAY, search_url=SEARCH)
        assert (post.source_ref, post.link_kind) == (f"ck:{KEY}", "profile"), copied


def test_without_an_author_link_the_card_links_to_the_search_and_without_either_it_is_a_parse_error() -> None:
    post = post_from_raw(_sdui(author_profile_url=None), LIVE, TODAY, search_url=SEARCH)
    assert (post.url, post.link_kind, post.author_profile_url) == (SEARCH, "search", None)
    with pytest.raises(ParseError) as err:
        post_from_raw(_sdui(author_profile_url="  "), LIVE, TODAY)
    assert not isinstance(err.value, TextMissing) and "no post, author or search link" in str(err.value)


def test_a_company_author_and_a_site_relative_profile_link_resolve_like_a_browser_would() -> None:
    post = post_from_raw(_sdui(author_profile_url="/in/meera-iyer-oh/"), LIVE, TODAY)
    assert post.url == f"{LIVE}/in/meera-iyer-oh/" and post.link_kind == "profile"
    fixture = post_from_raw(_sdui(author_profile_url=f"{FIX}/in/meera-iyer-oh/"), FIX, TODAY)
    assert fixture.url == f"{FIX}/in/meera-iyer-oh/" and fixture.link_kind == "profile"
    company = post_from_raw(_sdui(author_profile_url=f"{LIVE}/company/briltus-academy/"), LIVE, TODAY)
    assert company.link_kind == "profile"


@pytest.mark.parametrize(
    ("key", "ok"),
    [
        (KEY, True),
        (f"  {KEY}  ", True),  # trimmed
        ("a" * 197, True),  # "ck:" + 197 = 200 = enquiry_sources.source_ref String(200)
        ("a" * 198, False),
        ("has space", False),
        ("tab\there", False),
        ("", False),
        ("   ", False),
        (None, False),
        (12345, False),
    ],
)
def test_component_key_must_be_non_empty_without_whitespace_and_fit_the_source_ref_column(key: object, ok: bool) -> None:
    assert (component_key_of(key) is not None) is ok
    ref = source_ref_of(_sdui(component_key=key), LIVE)
    if ok:
        assert ref == f"ck:{str(key).strip()}" and len(ref) <= 200
    else:
        assert ref is None
        with pytest.raises(ParseError):
            post_from_raw(_sdui(component_key=key), LIVE, TODAY, search_url=SEARCH)


# --------------------------------------------------------------------------- whole page
def test_parse_cards_counts_text_less_containers_as_text_misses_not_parse_errors() -> None:
    raws = [
        _sdui(component_key="K1"),
        _sdui(component_key="K2", text=None),  # an image-only post / unreadable text
        _sdui(component_key="K3", text="  \n … more"),
        _sdui(component_key="K3", text=""),  # the same text-less container re-rendered: one miss
        _sdui(component_key=None, text="no ref at all"),  # a parse error
        _sdui(component_key="K4", author_headline=None),
    ]
    parsed = parse_cards(raws, LIVE, TODAY, cap=15, search_url=SEARCH)
    assert [p.source_ref for p in parsed.posts] == ["ck:K1", "ck:K4"]
    assert parsed.field_misses == {"text": 2, "author_headline": 1}
    assert list(parsed.field_misses)[0] == "text"  # listed first
    assert parsed.parse_errors == 1 and "component key" in parsed.errors[0]
    assert parsed.cards_found == 6 and parsed.strategy == "sdui"
    assert all(p.search_url == SEARCH for p in parsed.posts)


def test_a_page_of_only_text_less_containers_parses_to_zero_posts_with_text_misses() -> None:
    parsed = parse_cards([_sdui(component_key=f"K{i}", text=None) for i in range(3)], LIVE, TODAY)
    assert (parsed.posts, parsed.parse_errors, parsed.field_misses) == ([], 0, {"text": 3})


def test_a_container_text_less_once_and_readable_on_a_re_render_is_one_post_and_no_miss() -> None:
    parsed = parse_cards([_sdui(text=None), _sdui()], LIVE, TODAY)
    assert len(parsed.posts) == 1 and parsed.field_misses == {}


def test_parse_cards_dedupes_by_source_ref_and_caps_in_dom_order() -> None:
    raws = [
        _sdui(component_key="A"),
        _sdui(component_key="A", text="re-rendered, slightly different"),
        _sdui(component_key="B", urn="urn:li:activity:9"),
        _sdui(component_key="C", copied_url=f"{LIVE}/feed/update/urn:li:activity:9/"),  # same URN as B
        _sdui(component_key="D"),
    ]
    parsed = parse_cards(raws, LIVE, TODAY)
    assert [p.source_ref for p in parsed.posts] == ["ck:A", "urn:li:activity:9", "ck:D"]
    assert [p.link_kind for p in parsed.posts] == ["profile", "post", "profile"]
    assert parse_cards(raws, LIVE, TODAY, cap=2).posts[-1].source_ref == "urn:li:activity:9"


def test_the_strategy_comes_from_the_raws() -> None:
    assert parse_cards([_sdui()], LIVE, TODAY).strategy == "sdui"
    assert parse_cards([_sdui(strategy="urn", urn="urn:li:activity:1")], LIVE, TODAY).strategy == "urn"
    assert parse_cards([], LIVE, TODAY).strategy == "none"


# --------------------------------------------------------------------------- selector contract
def test_the_sdui_dom_contract_values_are_exactly_the_spec() -> None:
    assert S.SDUI_POST == '[data-view-name="feed-full-update"]'
    assert S.SDUI_LISTITEM == '[role="listitem"][componentkey]'
    assert S.SDUI_KEY_ATTR == "componentkey"
    assert S.SDUI_ACTOR == '[data-view-name="feed-actor"]'
    assert S.SDUI_TEXT == ('[data-view-name="feed-commentary"]', '[data-testid="expandable-text-box"]')
    assert S.SDUI_EXPAND == '[data-testid="expandable-text-button"]'
    assert S.SDUI_TEXT_EXCLUDE == ('[data-view-name="feed-update-image"]', '[data-view-name="feed-article"]',
                                   '[data-view-name="feed-document-container"]')
    assert S.SDUI_CONTROL_MENU == '[data-view-name="feed-control-menu"]'
    assert S.COPY_LINK_TEXT == "copy link to post"
    assert S.SDUI_SCROLL_CONTAINER == '[data-testid="lazy-column"]'
    assert S.SDUI_SCREEN == '[data-sdui-screen*="SearchResultsContent"]'
    assert S.SEE_MORE_ARIA_SELECTORS[0] == S.SDUI_EXPAND  # the SDUI "…more" is tried first


@pytest.mark.parametrize(
    ("value", "key"),
    [
        (f"expanded{KEY}FeedType_FLAGSHIP_SEARCH", KEY),
        ("expandedabcFeedType_MAIN_FEED", "abc"),
        (KEY, None),  # the inner key itself
        ("expandedFeedType_FLAGSHIP_SEARCH", None),
        ("00000000-aaaa-4bbb-8ccc-000000000001", None),
    ],
)
def test_the_listitem_key_regex_extracts_the_key(value: str, key: str | None) -> None:
    match = re.match(S.SDUI_LISTITEM_KEY_RE, value)
    assert (match.group(1) if match else None) == key
