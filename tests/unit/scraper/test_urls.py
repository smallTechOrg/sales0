from urllib.parse import parse_qs, urlsplit

import pytest

from sales0.config import Settings
from sales0.scraper.urls import FIXTURE_PREFIX, base_url, feed_url, login_url, permalink, search_url


def test_base_url_is_real_linkedin_in_live_mode() -> None:
    assert base_url(Settings(linkedin_mode="live")) == "https://www.linkedin.com"


def test_base_url_points_at_own_fixture_routes_in_fixture_mode() -> None:
    settings = Settings(linkedin_mode="fixture", host="127.0.0.1", port=8766)
    assert base_url(settings) == "http://127.0.0.1:8766/__fixtures/linkedin"
    assert FIXTURE_PREFIX == "/__fixtures/linkedin"


def test_search_url_encodes_keyword_and_date_filter() -> None:
    url = search_url("https://www.linkedin.com", "  AI workshop & more ", "past-week")
    parts = urlsplit(url)
    assert parts.path == "/search/results/content/"
    query = parse_qs(parts.query)
    assert query["keywords"] == ["AI workshop & more"]
    assert query["datePosted"] == ['"past-week"']
    assert query["sortBy"] == ['"date_posted"']


@pytest.mark.parametrize("bad", ["", "   "])
def test_search_url_rejects_blank_keyword(bad: str) -> None:
    with pytest.raises(ValueError):
        search_url("https://www.linkedin.com", bad, "past-week")


def test_search_url_rejects_unknown_date_filter() -> None:
    with pytest.raises(ValueError):
        search_url("https://www.linkedin.com", "ai", "past-year")


def test_feed_login_and_permalink_urls_are_relative_to_base() -> None:
    base = "http://127.0.0.1:1/__fixtures/linkedin"
    assert feed_url(base) == f"{base}/feed/"
    assert login_url(base) == f"{base}/login"
    assert permalink(base, "urn:li:activity:1") == f"{base}/feed/update/urn:li:activity:1/"
