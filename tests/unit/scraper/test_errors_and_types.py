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
        urn="urn:li:activity:1", url="u", author_name=None, author_profile_url=None,
        author_headline=None, posted_relative=None, posted_date=date(2026, 1, 1), text="t",
    )
    result = KeywordScrapeResult(keyword="k", posts=[post], scrolls=0, parse_errors=0, drift_warning=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        post.text = "x"  # type: ignore[misc]
    assert result.posts[0] is post
    assert SessionCheckResult(logged_in=True, reason="ok", url="u").logged_in
    assert ConnectResult(connected=False, reason="timeout", detail="Login not completed — try again").detail
