from datetime import date

import pytest

from sales0.scraper import selectors
from sales0.scraper.parsing import (
    EXTRACT_ARGS,
    EXTRACT_JS,
    URN_RE,
    ParseError,
    TextMissing,
    clean_text,
    drift_detected,
    field_misses_of,
    parse_cards,
    post_from_raw,
    see_more_args,
    urn_from_href,
)

BASE = "https://www.linkedin.com"
TODAY = date(2026, 10, 27)


def _raw(**over: object) -> dict:
    raw = {
        "urn": "urn:li:activity:7300000000000000001",
        "author_name": "  Priya Menon ",
        "author_profile_url": "/in/priya-menon?miniProfileUrn=x",
        "author_headline": "Head of L&D at Acme Logistics",
        "posted_relative": "1d •",
        "text": "Line one   \n\n\n\nLine two\n…see more",
    }
    raw.update(over)
    return raw


def test_post_from_raw_builds_a_clean_scraped_post() -> None:
    post = post_from_raw(_raw(), BASE, TODAY)
    assert post.urn == post.source_ref == "urn:li:activity:7300000000000000001"
    assert post.link_kind == "post" and post.search_url is None
    assert post.url == f"{BASE}/feed/update/urn:li:activity:7300000000000000001/"
    assert post.author_name == "Priya Menon"
    assert post.author_profile_url == "https://www.linkedin.com/in/priya-menon"
    assert post.author_headline == "Head of L&D at Acme Logistics"
    assert post.posted_relative == "1d"
    assert post.posted_date == date(2026, 10, 26)
    assert post.text == "Line one\n\nLine two"


def test_post_from_raw_keeps_unicode_and_emoji_intact() -> None:
    text = "हमें AI एजेंट्स वर्कशॉप चाहिए 🚀 Bengaluru"
    assert post_from_raw(_raw(text=text), BASE, TODAY).text == text


def test_post_from_raw_tolerates_missing_optional_fields() -> None:
    post = post_from_raw(
        _raw(author_name=None, author_profile_url="", author_headline="  ", posted_relative=None), BASE, TODAY
    )
    assert post.author_name is None
    assert post.author_profile_url is None
    assert post.author_headline is None
    assert post.posted_relative is None
    assert post.posted_date is None


@pytest.mark.parametrize(
    "urn", [None, "", "urn:li:group:1", "urn:li:fsd_profile:1", "activity:123", "urn:li:activity:abc", "urn:li:share:"]
)
def test_post_from_raw_rejects_a_missing_or_foreign_urn_without_a_component_key(urn: object) -> None:
    with pytest.raises(ParseError) as err:
        post_from_raw(_raw(urn=urn), BASE, TODAY)
    assert not isinstance(err.value, TextMissing)  # no ref at all is a real parse error


@pytest.mark.parametrize("text", [None, "", "   \n ", "…see more", "…more", "… more"])
def test_post_from_raw_signals_empty_text_with_a_ref_as_text_missing(text: object) -> None:
    with pytest.raises(TextMissing):  # a ParseError subclass; parse_cards counts it as field_misses["text"]
        post_from_raw(_raw(text=text), BASE, TODAY)


def test_clean_text_strips_see_more_and_see_less_toggles() -> None:
    assert clean_text("Hello\n… see more") == "Hello"
    assert clean_text("Hello\nsee less") == "Hello"
    assert clean_text("Hello\n…more") == "Hello"
    assert clean_text("Hello\n…show more") == "Hello"
    assert clean_text("Hello\r\nWorld  ") == "Hello\nWorld"
    assert clean_text("Learn more about us") == "Learn more about us"  # only a whole-line toggle is dropped
    # Phase 1.2: SDUI renders the "…more" button inline at the end of the text
    assert clean_text("We need a trainer … more") == "We need a trainer"
    assert clean_text("We need a trainer…more") == "We need a trainer"
    assert clean_text("We need a trainer...see more") == "We need a trainer"
    assert clean_text("Tell me more") == "Tell me more"  # no ellipsis: a real word


@pytest.mark.parametrize(
    "urn", ["urn:li:activity:7300000000000000001", "urn:li:ugcPost:7300000000000000099", "urn:li:share:7300000000000000077"]
)
def test_post_from_raw_accepts_activity_ugcpost_and_share_urns(urn: str) -> None:
    post = post_from_raw(_raw(urn=f"  {urn} "), BASE, TODAY)
    assert post.urn == urn  # whitespace-normalised, so the same card always dedupes to one key
    assert post.url == f"{BASE}/feed/update/{urn}/"


# --------------------------------------------------------------------------- URN from links
@pytest.mark.parametrize(
    ("href", "urn"),
    [
        ("/feed/update/urn:li:activity:7300000000000000008/", "urn:li:activity:7300000000000000008"),
        (f"{BASE}/feed/update/urn:li:ugcPost:7311/?utm=x", "urn:li:ugcPost:7311"),
        ("/feed/update/urn%3Ali%3Ashare%3A7322/", "urn:li:share:7322"),  # URL-encoded
        (f"{BASE}/posts/anita-desai-qw_claude-code-activity-7300000000000000010-AbCd/", "urn:li:activity:7300000000000000010"),
        ("/posts/briltus_ai-agents-ugcPost-7344-x9Zq?trk=public", "urn:li:ugcPost:7344"),
        ("/posts/a-b-c-share-7355-Q", "urn:li:share:7355"),
        ("/in/priya-menon/", None),
        ("/posts/no-id-here/", None),
        ("/feed/update/urn:li:group:1/", None),
        ("", None),
        (None, None),
    ],
)
def test_urn_from_href_reads_feed_update_and_posts_slug_links(href: object, urn: str | None) -> None:
    assert urn_from_href(href) == urn


def test_post_from_raw_falls_back_to_the_post_link_when_the_card_has_no_urn_attribute() -> None:
    href = f"{BASE}/posts/sneha-k_genai-activity-7390000000000000106-Xk9P/"
    post = post_from_raw(_raw(urn=None, post_href=href), BASE, TODAY)
    assert post.urn == "urn:li:activity:7390000000000000106"
    assert post.url == f"{BASE}/feed/update/urn:li:activity:7390000000000000106/"
    with pytest.raises(ParseError):
        post_from_raw(_raw(urn=None, post_href="/in/someone/"), BASE, TODAY)


# --------------------------------------------------------------------------- whole page
def test_parse_cards_dedupes_caps_counts_errors_and_field_misses() -> None:
    raws = [
        _raw(urn="urn:li:activity:1"),
        _raw(urn="urn:li:activity:1"),  # LinkedIn re-rendered the same card: not an error
        _raw(urn="urn:li:group:9"),  # parse error
        _raw(urn="urn:li:activity:2", author_headline=None, posted_relative=""),
        _raw(urn="urn:li:activity:3", text="   "),  # Phase 1.2: a text miss, not a parse error
        _raw(urn="urn:li:activity:4", author_headline=None),
    ]
    parsed = parse_cards(raws, BASE, TODAY)
    assert [p.urn for p in parsed.posts] == ["urn:li:activity:1", "urn:li:activity:2", "urn:li:activity:4"]
    assert parsed.parse_errors == 1 and len(parsed.errors) == 1
    assert parsed.cards_found == 6
    assert parsed.field_misses == {"text": 1, "author_headline": 2, "posted_relative": 1}
    assert parsed.strategy == "urn"  # raws without a strategy are URN-layout cards

    capped = parse_cards(raws, BASE, TODAY, cap=1)
    assert [p.urn for p in capped.posts] == ["urn:li:activity:1"] and capped.field_misses == {}


def test_parse_cards_of_an_empty_page_is_empty() -> None:
    parsed = parse_cards([], BASE, TODAY, cap=15)
    assert (parsed.posts, parsed.parse_errors, parsed.cards_found, parsed.field_misses) == ([], 0, 0, {})
    assert parsed.strategy == "none"


def test_field_misses_reports_only_missing_optional_fields() -> None:
    posts = [post_from_raw(_raw(urn=f"urn:li:activity:{i}", author_name=None if i else "X"), BASE, TODAY) for i in range(3)]
    assert field_misses_of(posts) == {"author_name": 2}
    assert field_misses_of([]) == {}


@pytest.mark.parametrize(
    ("posts", "cards", "no_results", "drift"),
    [
        (0, 0, False, True),  # logged-in page with neither cards nor the no-results marker
        (0, 3, False, True),  # cards found but none parsed
        (0, 3, True, True),
        (0, 0, True, False),  # a genuine "No results found"
        (2, 2, False, False),
        (1, 0, False, False),
    ],
)
def test_drift_is_zero_posts_with_cards_or_without_a_no_results_marker(
    posts: int, cards: int, no_results: bool, drift: bool
) -> None:
    assert drift_detected(posts, cards, no_results) is drift


# --------------------------------------------------------------------------- selector contract
def test_card_selectors_cover_every_urn_prefix_on_both_attributes() -> None:
    for prefix in ("urn:li:activity:", "urn:li:ugcPost:", "urn:li:share:"):
        assert URN_RE.match(prefix + "123")
        for attr in ("data-urn", "data-id"):
            assert f'[{attr}^="{prefix}"]' in selectors.CARD_ATTR_SELECTORS
            assert f'a[href*="/feed/update/{prefix}"]' in selectors.POST_LINK_SELECTORS


def test_class_selectors_survive_only_as_last_fallbacks() -> None:
    assert selectors.TEXT_SELECTORS[-1] == ".update-components-text"
    sdui = (selectors.SDUI_POST, selectors.SDUI_LISTITEM, selectors.SDUI_ACTOR, selectors.SDUI_EXPAND,
            selectors.SDUI_CONTROL_MENU, selectors.SDUI_SCROLL_CONTAINER, selectors.SDUI_SCREEN,
            *selectors.SDUI_TEXT, *selectors.SDUI_TEXT_EXCLUDE)
    for sel in selectors.TEXT_SELECTORS[:-1] + selectors.CARD_ATTR_SELECTORS + selectors.POST_LINK_SELECTORS + sdui:
        assert "." not in sel.split("[")[0], sel  # no class selector outside the legacy fallback
    for name in ("LEGACY_AUTHOR_NAME", "LEGACY_AUTHOR_PROFILE_LINK", "LEGACY_AUTHOR_HEADLINE", "LEGACY_RELATIVE_TIME",
                 "LEGACY_SEE_MORE", "LEGACY_NO_RESULTS"):
        assert getattr(selectors, name).startswith((".", "a.", "button."))


def test_extract_and_see_more_scripts_receive_every_selector_they_use() -> None:
    import re

    from sales0.scraper.parsing import CARD_ARGS, COUNT_CARDS_JS

    for key in set(re.findall(r"sel\.(\w+)", EXTRACT_JS)):
        assert key in EXTRACT_ARGS, key
    for key in set(re.findall(r"sel\.(\w+)", COUNT_CARDS_JS)):
        assert key in CARD_ARGS, key
    from sales0.scraper.parsing import SEE_MORE_JS

    args = see_more_args(3)
    assert args["n"] == 3 and see_more_args(-2)["n"] == 0
    for key in set(re.findall(r"sel\.(\w+)", SEE_MORE_JS)):
        assert key in args, key


def test_signal_script_receives_every_selector_it_uses() -> None:
    import re

    from sales0.scraper.detection import CONTENT_READY_JS, SIGNAL_ARGS, SIGNALS_JS

    for script in (SIGNALS_JS, CONTENT_READY_JS):
        for key in set(re.findall(r"sel\.(\w+)", script)):
            assert key in SIGNAL_ARGS, key
