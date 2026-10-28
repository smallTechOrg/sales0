"""``apply_links``: how a captured permalink upgrades a scanned post (pure)."""

from __future__ import annotations

from sales0.scraper.search import apply_links
from sales0.scraper.types import ScrapedPost

BASE = "https://www.linkedin.com"
URN = "urn:li:activity:7300000000000000005"


def _post(ref: str, url: str = f"{BASE}/in/a/") -> ScrapedPost:
    return ScrapedPost(ref, url, "A", f"{BASE}/in/a/", "h", "1d", None, f"text {ref}", link_kind="profile")


def test_a_link_with_a_urn_replaces_the_ck_ref_and_makes_the_post_link_a_post() -> None:
    out = apply_links([_post("ck:K1"), _post("ck:K2")], {"ck:K1": f"{BASE}/feed/update/{URN}/"})
    assert (out[0].source_ref, out[0].url, out[0].link_kind) == (URN, f"{BASE}/feed/update/{URN}/", "post")
    assert out[1].source_ref == "ck:K2" and out[1].link_kind == "profile"


def test_a_link_without_a_urn_keeps_the_ref() -> None:
    out = apply_links([_post("ck:K1")], {"ck:K1": f"{BASE}/posts/someone_hello-world?x"})
    assert out[0].source_ref == "ck:K1" and out[0].link_kind == "post"


def test_posts_that_collapse_to_the_same_urn_are_deduplicated() -> None:
    link = f"{BASE}/feed/update/{URN}/"
    out = apply_links([_post("ck:K1"), _post("ck:K2")], {"ck:K1": link, "ck:K2": link})
    assert [p.source_ref for p in out] == [URN]


def test_no_links_is_the_identity() -> None:
    posts = [_post("ck:K1"), _post("ck:K2")]
    assert apply_links(posts, {}) == posts
