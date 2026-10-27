"""services.runs.keyword_outcome (pure; architecture.md data flow step 3): drift and field
misses become a keyword ``warning`` with a user-readable message; smaller misses and parse
errors are still written to the Message column (nothing is silent)."""

from __future__ import annotations

from datetime import date

import pytest

from sales0.scraper.types import KeywordScrapeResult, ScrapedPost
from sales0.services.runs import keyword_outcome


def _post(n: int) -> ScrapedPost:
    urn = f"urn:li:activity:7{n:018d}"
    return ScrapedPost(
        urn=urn,
        url=f"https://www.linkedin.com/feed/update/{urn}/",
        author_name="A",
        author_profile_url="https://www.linkedin.com/in/a/",
        author_headline="H",
        posted_relative="1d",
        posted_date=date(2026, 10, 26),
        text=f"post {n}",
    )


def _result(n_posts: int, *, drift: bool = False, cards: int = 0, parse_errors: int = 0,
            misses: dict[str, int] | None = None, no_results: bool = False) -> KeywordScrapeResult:
    return KeywordScrapeResult(
        keyword="ai workshop",
        posts=[_post(i) for i in range(n_posts)],
        scrolls=1,
        parse_errors=parse_errors,
        drift_warning=drift,
        cards_found=cards,
        no_results=no_results,
        field_misses=dict(misses or {}),
    )


def test_drift_is_a_warning_that_names_the_result_card_count() -> None:
    status, message = keyword_outcome(_result(0, drift=True, cards=5))
    assert status == "warning"
    assert message == "No posts parsed — LinkedIn layout may have changed (5 result cards found)"


def test_drift_with_a_single_card_uses_the_singular() -> None:
    assert keyword_outcome(_result(0, drift=True, cards=1)) == (
        "warning", "No posts parsed — LinkedIn layout may have changed (1 result card found)")


def test_drift_wins_over_field_misses() -> None:
    status, message = keyword_outcome(_result(0, drift=True, cards=3, misses={"author_name": 3}))
    assert status == "warning" and (message or "").startswith("No posts parsed")


def test_author_missing_on_half_of_the_posts_is_a_warning() -> None:
    status, message = keyword_outcome(_result(4, misses={"author_name": 2}))
    assert status == "warning"
    assert message == "Parsed 4 posts, but couldn't read author on 2 — LinkedIn layout may have changed"


def test_several_fields_over_the_threshold_are_listed_in_the_spec_order() -> None:
    status, message = keyword_outcome(
        _result(3, misses={"author_profile_url": 3, "posted_relative": 2, "author_headline": 2, "author_name": 0}))
    assert status == "warning"
    assert message == ("Parsed 3 posts, but couldn't read headline on 2, post time on 2, profile link on 3 "
                       "— LinkedIn layout may have changed")


def test_field_miss_warning_also_lists_unreadable_cards() -> None:
    _status, message = keyword_outcome(_result(2, parse_errors=1, misses={"author_name": 2}))
    assert message == ("Parsed 2 posts, but couldn't read author on 2 — LinkedIn layout may have changed"
                       " · 1 card couldn't be read")


@pytest.mark.parametrize("n_posts", [0, 1])
def test_fewer_than_two_posts_never_trip_the_field_threshold(n_posts: int) -> None:
    status, message = keyword_outcome(_result(n_posts, misses={"author_name": n_posts}))
    assert status == "done"
    if n_posts:
        assert message == "author missing on 1/1"
    else:
        assert message is None


def test_smaller_misses_and_parse_errors_stay_done_but_are_written_down() -> None:
    status, message = keyword_outcome(_result(8, parse_errors=2, misses={"author_headline": 1}))
    assert (status, message) == ("done", "2 cards couldn't be read · headline missing on 1/8")


def test_a_clean_result_is_done_with_no_message() -> None:
    assert keyword_outcome(_result(4)) == ("done", None)


def test_an_honest_no_results_page_is_done_with_no_message() -> None:
    assert keyword_outcome(_result(0, no_results=True)) == ("done", None)


def test_unknown_field_names_and_zero_counts_are_ignored() -> None:
    assert keyword_outcome(_result(4, misses={"bogus": 4, "author_name": 0})) == ("done", None)
