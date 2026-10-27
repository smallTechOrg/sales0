from datetime import date

import pytest

from sales0.scraper.parsing import ParseError, clean_text, post_from_raw

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
    assert post.urn == "urn:li:activity:7300000000000000001"
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
def test_post_from_raw_rejects_a_missing_or_foreign_urn(urn: object) -> None:
    with pytest.raises(ParseError):
        post_from_raw(_raw(urn=urn), BASE, TODAY)


@pytest.mark.parametrize("text", [None, "", "   \n ", "…see more"])
def test_post_from_raw_rejects_empty_text(text: object) -> None:
    with pytest.raises(ParseError):
        post_from_raw(_raw(text=text), BASE, TODAY)


def test_clean_text_strips_see_more_and_see_less_toggles() -> None:
    assert clean_text("Hello\n… see more") == "Hello"
    assert clean_text("Hello\nsee less") == "Hello"
    assert clean_text("Hello\r\nWorld  ") == "Hello\nWorld"


@pytest.mark.parametrize(
    "urn", ["urn:li:activity:7300000000000000001", "urn:li:ugcPost:7300000000000000099", "urn:li:share:7300000000000000077"]
)
def test_post_from_raw_accepts_activity_ugcpost_and_share_urns(urn: str) -> None:
    post = post_from_raw(_raw(urn=f"  {urn} "), BASE, TODAY)
    assert post.urn == urn  # whitespace-normalised, so the same card always dedupes to one key
    assert post.url == f"{BASE}/feed/update/{urn}/"


def test_result_card_selector_covers_every_accepted_urn_prefix() -> None:
    from sales0.scraper import selectors
    from sales0.scraper.parsing import URN_RE

    for prefix in ("urn:li:activity:", "urn:li:ugcPost:", "urn:li:share:"):
        assert f'div[data-urn^="{prefix}"]' in selectors.RESULT_CARD
        assert URN_RE.match(prefix + "123")
