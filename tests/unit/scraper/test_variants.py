"""Phase 1.5: per-phrase search variant planning (pure)."""

from __future__ import annotations

from datetime import date

from sales0.scraper.variants import hashtag_form, plan_passes, quoted_form

DAY = date(2026, 10, 28)


def _plan(phrase: str = "ai trainer requirement", *, variants: bool = True, max_passes: int = 6, day: date = DAY):
    return plan_passes(phrase, ("past-week", "past-month"), "latest", variants=variants, max_passes=max_passes, today=day)


def test_without_variants_the_plan_is_exactly_the_configured_filters() -> None:
    plan = _plan(variants=False)
    assert [(p.query, p.date_filter, p.sort, p.kind) for p in plan] == [
        ("ai trainer requirement", "past-week", "latest", "baseline"),
        ("ai trainer requirement", "past-month", "latest", "baseline"),
    ]


def test_baseline_first_then_capped_unique_variants() -> None:
    plan = _plan(max_passes=6)
    assert [p.kind for p in plan[:2]] == ["baseline", "baseline"]
    assert len(plan) == 6
    assert len({(p.query, p.date_filter, p.sort) for p in plan}) == 6


def test_all_variant_kinds_exist_when_uncapped() -> None:
    kinds = {p.kind for p in _plan("generative ai workshop", max_passes=12)}
    assert {"baseline", "other_sort", "quoted", "hashtag", "suffix:urgent", "suffix:requirement", "suffix:enquiry", "narrower"} <= kinds
    queries = {p.query for p in _plan("generative ai workshop", max_passes=12)}
    assert '"generative ai workshop"' in queries and "#generativeai" in queries
    assert "generative ai workshop urgent" in queries


def test_rotation_changes_with_the_day_but_is_deterministic() -> None:
    a, b = _plan(day=DAY), _plan(day=date(2026, 10, 29))
    assert a == _plan(day=DAY)
    assert [p.kind for p in a[2:]] != [p.kind for p in b[2:]]


def test_a_suffix_already_in_the_phrase_is_not_duplicated() -> None:
    queries = [p.query for p in _plan("ai trainer requirement", max_passes=12)]
    assert "ai trainer requirement requirement" not in queries


def test_helpers() -> None:
    assert hashtag_form("AI Trainer needed") == "#aitrainer"
    assert hashtag_form("trainer") is None
    assert quoted_form('say "hi" there') == '"say hi there"'
    assert quoted_form("single") is None


def test_max_passes_never_drops_below_one_and_phrase_is_normalised() -> None:
    plan = plan_passes("  ai   trainer ", ("past-week",), "relevance", variants=True, max_passes=1, today=DAY)
    assert len(plan) == 1 and plan[0].query == "ai trainer" and plan[0].sort == "relevance"
