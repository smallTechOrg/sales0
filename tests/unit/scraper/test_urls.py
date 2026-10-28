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


# --------------------------------------------------------------------------- Phase 1.2 pure helpers
from sales0.scraper.urls import (  # noqa: E402
    canonical_post_url,
    link_kind_of,
    linkedin_path,
    profile_slug,
    same_site,
    urn_from_href,
)

LIVE = "https://www.linkedin.com"
FIX = "http://127.0.0.1:43123/__fixtures/linkedin"


@pytest.mark.parametrize(
    ("copied", "base", "expected"),
    [
        # a URN in the copied link → the clean permalink
        (f"{LIVE}/posts/arjun-rao_ai-agents-activity-7300000000000000005-AbCd?utm_source=share&utm_medium=member_desktop&rcm=ACoAAX",
         LIVE, ("urn:li:activity:7300000000000000005", f"{LIVE}/feed/update/urn:li:activity:7300000000000000005/")),
        (f"{LIVE}/feed/update/urn:li:ugcPost:7311/?trk=x#c", LIVE, ("urn:li:ugcPost:7311", f"{LIVE}/feed/update/urn:li:ugcPost:7311/")),
        (f"{FIX}/posts/p5-slug_x-activity-7300000000000000005-AbCd?utm_source=share&rcm=ACoAAFixture",
         FIX, ("urn:li:activity:7300000000000000005", f"{FIX}/feed/update/urn:li:activity:7300000000000000005/")),
        ("https://linkedin.com/feed/update/urn:li:share:9/", LIVE, ("urn:li:share:9", f"{LIVE}/feed/update/urn:li:share:9/")),
        # a same-site post path without a URN → that URL, query and fragment dropped
        (f"{LIVE}/posts/someone_short-slug?utm_source=share&rcm=x#frag", LIVE, (None, f"{LIVE}/posts/someone_short-slug")),
        (f"{FIX}/feed/update/opaque-id/?utm_medium=x", FIX, (None, f"{FIX}/feed/update/opaque-id/")),
        # site-relative links resolve against the base
        ("/feed/update/urn:li:activity:42/", LIVE, ("urn:li:activity:42", f"{LIVE}/feed/update/urn:li:activity:42/")),
        # never stored: other sites, non-post paths, junk
        ("https://evil.example/posts/x-activity-1-a", LIVE, (None, None)),
        ("https://lnkd.in/abc", LIVE, (None, None)),
        (f"{LIVE}/in/arjun-rao/", LIVE, (None, None)),
        (f"{LIVE}/search/results/content/?keywords=x", LIVE, (None, None)),
        (f"http://127.0.0.1:9/__fixtures/linkedin/posts/x-activity-1-a", FIX, (None, None)),  # other port
        ("javascript:alert(1)", LIVE, (None, None)),
        ("Link copied to clipboard.", LIVE, (None, None)),
        ("", LIVE, (None, None)),
        (None, LIVE, (None, None)),
        (42, LIVE, (None, None)),
    ],
)
def test_canonical_post_url_keeps_only_clean_same_site_post_links(copied: object, base: str, expected: tuple) -> None:
    assert canonical_post_url(copied, base) == expected


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        (f"{LIVE}/feed/update/urn:li:activity:1/", "post"),
        (f"{FIX}/feed/update/urn:li:activity:1/", "post"),
        (f"{LIVE}/posts/a_b-activity-1-x", "post"),
        (f"{LIVE}/in/priya-menon", "profile"),
        (f"{FIX}/in/priya-menon/", "profile"),
        (f"{LIVE}/company/briltus-academy/", "profile"),
        (f"{LIVE}/search/results/content/?keywords=ai", "search"),
        (f"{FIX}/search/results/content/?keywords=ai", "search"),
        (f"{LIVE}/showcase/x/", "other"),
        (f"{LIVE}/", "other"),
        ("https://example.com/in-depth/", "other"),
        ("/in/relative-slug/", "profile"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_link_kind_of_classifies_a_card_link_by_path(url: str | None, kind: str | None) -> None:
    assert link_kind_of(url) == kind


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        (f"{LIVE}/in/priya-menon", "in/priya-menon"),
        (f"{LIVE}/in/Priya-Menon/?miniProfileUrn=x", "in/priya-menon"),
        (f"{FIX}/in/arjun-rao/", "in/arjun-rao"),
        (f"{LIVE}/company/Briltus-Academy/posts/", "company/briltus-academy"),
        (f"{LIVE}/in/%E0%A4%85%E0%A4%B0%E0%A5%8D%E0%A4%9C%E0%A5%81%E0%A4%A8-1/", "in/अर्जुन-1"),  # URL-decoded
        ("/in/relative/", "in/relative"),
        (f"{LIVE}/feed/update/urn:li:activity:1/", None),
        (f"{LIVE}/in/", None),
        (f"{LIVE}/search/results/content/?keywords=in/x", None),  # the query is never a profile
        ("", None),
        (None, None),
    ],
)
def test_profile_slug_is_the_lowercased_decoded_in_or_company_slug(url: str | None, slug: str | None) -> None:
    assert profile_slug(url) == slug


def test_urn_from_href_and_site_rules_live_in_urls_and_are_re_exported() -> None:
    from sales0.scraper import detection, parsing

    assert parsing.urn_from_href is urn_from_href
    assert detection.same_site is same_site and detection.linkedin_path is linkedin_path
    assert urn_from_href("/feed/update/urn%3Ali%3Ashare%3A7322/") == "urn:li:share:7322"
    assert linkedin_path(f"{FIX}/feed/") == "/feed/" and linkedin_path("about:blank") is None
    assert same_site(f"{LIVE}/x", LIVE) and not same_site(f"{FIX}/x", LIVE)


def test_search_url_sort_latest_is_the_default_and_relevance_is_selectable() -> None:
    base = "https://www.linkedin.com"
    assert parse_qs(urlsplit(search_url(base, "ai", "past-month")).query)["sortBy"] == ['"date_posted"']
    assert parse_qs(urlsplit(search_url(base, "ai", "past-month", "latest")).query)["sortBy"] == ['"date_posted"']
    assert parse_qs(urlsplit(search_url(base, "ai", "past-month", "relevance")).query)["sortBy"] == ['"relevance"']
    assert parse_qs(urlsplit(search_url(base, "ai", "past-month", "relevance")).query)["datePosted"] == ['"past-month"']


def test_search_url_rejects_an_unknown_sort() -> None:
    with pytest.raises(ValueError):
        search_url("https://www.linkedin.com", "ai", "past-week", "top")  # type: ignore[arg-type]
