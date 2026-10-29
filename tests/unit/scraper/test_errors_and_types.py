import dataclasses
from datetime import date

import pytest

from sales0.scraper.errors import AccountSafetyStop, ScrapeError
from sales0.scraper.types import ConnectResult, KeywordScrapeResult, ScrapedPost, SessionCheckResult


def test_account_safety_stop_carries_kind_and_url() -> None:
    stop = AccountSafetyStop("checkpoint", "https://www.linkedin.com/checkpoint/challenge/")
    assert stop.kind == "checkpoint"
    assert stop.url.endswith("/checkpoint/challenge/")
    assert "checkpoint" in str(stop)
    assert not isinstance(stop, ScrapeError)  # a stop must never be swallowed as a keyword error


def test_account_safety_stop_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        AccountSafetyStop("slow", "x")  # type: ignore[arg-type]


def test_scraper_types_are_frozen_dataclasses() -> None:
    post = ScrapedPost(
        source_ref="urn:li:activity:1", url="u", author_name=None, author_profile_url=None,
        author_headline=None, posted_relative=None, posted_date=date(2026, 1, 1), text="t",
    )
    result = KeywordScrapeResult(keyword="k", posts=[post], scrolls=0, parse_errors=0, drift_warning=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        post.text = "x"  # type: ignore[misc]
    assert result.posts[0] is post
    assert SessionCheckResult(logged_in=True, reason="ok", url="u").logged_in
    assert ConnectResult(connected=False, reason="timeout", detail="Login not completed — try again").detail


def _post(ref: str, **over: object) -> ScrapedPost:
    fields: dict = dict(
        source_ref=ref, url="https://www.linkedin.com/in/someone", author_name=None, author_profile_url=None,
        author_headline=None, posted_relative=None, posted_date=None, text="t",
    )
    fields.update(over)
    return ScrapedPost(**fields)


@pytest.mark.parametrize(
    ("ref", "urn"),
    [
        ("urn:li:activity:7300000000000000001", "urn:li:activity:7300000000000000001"),
        ("urn:li:ugcPost:5", "urn:li:ugcPost:5"),
        ("ck:sxQ3v9LmP1aZ0k", None),
        ("ck:urn:li:activity:1-looks-like-one", None),  # only a ref that STARTS with urn:li: is a URN
    ],
)
def test_scraped_post_urn_is_the_source_ref_only_when_it_is_a_urn(ref: str, urn: str | None) -> None:
    assert _post(ref).urn == urn
    assert _post(ref).source_ref == ref


def test_scraped_post_phase_1_2_fields_default_to_a_post_link_without_provenance() -> None:
    post = _post("ck:K")
    assert (post.link_kind, post.search_url) == ("post", None)
    kinded = _post("ck:K", link_kind="profile", search_url="https://www.linkedin.com/search/results/content/?keywords=x")
    assert kinded.link_kind == "profile" and kinded.search_url.endswith("keywords=x")
    with pytest.raises(TypeError):
        ScrapedPost(urn="urn:li:activity:1", url="u", author_name=None, author_profile_url=None,  # type: ignore[call-arg]
                    author_headline=None, posted_relative=None, posted_date=None, text="t")


def test_keyword_scrape_result_phase_1_2_counters_default_to_none_and_zero() -> None:
    result = KeywordScrapeResult(keyword="k", posts=[], scrolls=0, parse_errors=0, drift_warning=False)
    assert (result.strategy, result.permalink_attempts, result.permalinks_found) == ("none", 0, 0)
    sdui = KeywordScrapeResult(keyword="k", posts=[], scrolls=1, parse_errors=0, drift_warning=False,
                               field_misses={"text": 2}, strategy="sdui", permalink_attempts=3, permalinks_found=1)
    assert (sdui.strategy, sdui.permalink_attempts, sdui.permalinks_found, sdui.field_misses) == ("sdui", 3, 1, {"text": 2})


def test_phase_1_4_result_fields_have_backward_compatible_defaults() -> None:
    from sales0.scraper.types import KeywordScrapeResult, LinkCaptureResult

    result = KeywordScrapeResult(keyword="k", posts=[], scrolls=0, parse_errors=0, drift_warning=False)
    assert (result.passes, result.expanded, result.stop_reason) == (1, 0, "cap")
    empty = LinkCaptureResult()
    assert empty.links == {} and empty.attempts == 0 and empty.found == 0
