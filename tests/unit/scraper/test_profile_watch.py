from __future__ import annotations

from datetime import date

import pytest

from sales0.scraper.profile_watch import own_posts
from sales0.scraper.types import ScrapedPost
from sales0.scraper.urls import profile_activity_url

BASE = "https://www.linkedin.com"


def test_activity_urls_per_profile_kind() -> None:
    assert profile_activity_url(BASE, "in/priya-menon-1") == f"{BASE}/in/priya-menon-1/recent-activity/all/"
    assert profile_activity_url(BASE, "company/acme") == f"{BASE}/company/acme/posts/"
    fx = "http://127.0.0.1:9/__fixtures/linkedin"
    assert profile_activity_url(fx, "in/x") == f"{fx}/in/x/recent-activity/all/"


@pytest.mark.parametrize("slug", ["", "in/", "pub/x", "in/a/b", "x"])
def test_activity_url_rejects_non_profile_slugs(slug: str) -> None:
    with pytest.raises(ValueError):
        profile_activity_url(BASE, slug)


def _p(ref: str, author_url: str | None) -> ScrapedPost:
    return ScrapedPost(source_ref=ref, url=f"{BASE}/in/x/", author_name="A", author_profile_url=author_url, author_headline=None,
                       posted_relative="1d", posted_date=date(2026, 10, 1), text="t", link_kind="profile")


def test_own_posts_keeps_own_and_author_less_and_skips_others() -> None:
    mine, foreign = own_posts(
        [_p("a", f"{BASE}/in/asha/"), _p("b", None), _p("c", f"{BASE}/in/someone-else/"), _p("d", f"{BASE}/in/ASHA/?x=1")],
        "in/asha", f"{BASE}/in/asha/",
    )
    assert [p.source_ref for p in mine] == ["a", "b", "d"] and foreign == 1
    assert mine[1].author_profile_url == f"{BASE}/in/asha/"  # attributed to the tracked profile
