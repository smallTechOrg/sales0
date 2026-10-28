"""fixscrape #11: curated hashtags, no quoting of long phrases, per-day cross-phrase query dedupe."""

from __future__ import annotations

from datetime import date

import pytest

from sales0.scraper import variants
from sales0.scraper.variants import hashtag_form, plan_passes, quoted_form

DAY = date(2026, 10, 28)


@pytest.fixture(autouse=True)
def _fresh_claims():
    variants.reset_day_claims()
    yield
    variants.reset_day_claims()


@pytest.mark.parametrize(
    "phrase",
    ["looking for AI trainer for our team", "hands-on GenAI bootcamp for engineers", "urgent trainer requirement AI",
     "need chatbot developer company", "hiring AI consultant"],
)
def test_hashtags_are_never_junk_two_word_prefixes(phrase: str) -> None:
    tag = hashtag_form(phrase)
    assert tag not in {"#lookingfor", "#handson", "#urgenttrainer", "#needchatbot", "#hiringai"}


def test_hashtags_come_from_a_curated_map() -> None:
    assert hashtag_form("Claude Code training for developers") == "#claudecode"
    assert hashtag_form("hands-on GenAI bootcamp for engineers") == "#generativeai"
    assert hashtag_form("looking for AI trainer") == "#aitrainer"
    assert hashtag_form("Microsoft Copilot training for our team") == "#copilot"
    assert hashtag_form("trainer chahiye") is None


def test_quoted_form_skips_phrases_over_four_words() -> None:
    assert quoted_form("ai training partner") == '"ai training partner"'
    assert quoted_form("one two three four") == '"one two three four"'
    assert quoted_form("one two three four five") is None
    queries = [p.query for p in plan_passes("AI workshop for leadership team now", ("past-week",), "latest",
                                            variants=True, max_passes=12, today=DAY)]
    assert not any(q.startswith('"') for q in queries)


def test_identical_variant_queries_across_phrases_are_planned_once_per_day() -> None:
    kw = dict(variants=True, max_passes=12, today=DAY)
    a = plan_passes("generative ai workshop", ("past-week",), "latest", **kw)
    b = plan_passes("generative ai training", ("past-week",), "latest", **kw)
    keys_a = {(p.query, p.date_filter, p.sort) for p in a if p.kind != "baseline"}
    keys_b = {(p.query, p.date_filter, p.sort) for p in b if p.kind != "baseline"}
    assert "#generativeai" in {k[0] for k in keys_a}
    assert not keys_a & keys_b  # the hashtag pass is not repeated for the second phrase
    # replanning the SAME phrase the same day is stable
    assert plan_passes("generative ai workshop", ("past-week",), "latest", **kw) == a
    # next day: claims reset
    c = plan_passes("generative ai training", ("past-week",), "latest", variants=True, max_passes=12, today=date(2026, 10, 29))
    assert "#generativeai" in {p.query for p in c}


def test_baseline_is_never_deduped_away() -> None:
    kw = dict(variants=True, max_passes=3, today=DAY)
    plan_passes("alpha beta gamma", ("past-week",), "latest", **kw)
    again = plan_passes("alpha beta gamma", ("past-week",), "latest", **kw)
    assert again[0].kind == "baseline"
