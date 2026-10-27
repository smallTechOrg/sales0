import random

import pytest

from sales0.config import Settings
from sales0.scraper.pacing import Pacer


def test_pacer_sleeps_a_random_duration_within_bounds() -> None:
    slept: list[float] = []
    pacer = Pacer(2.0, 5.0, random.Random(7), slept.append)
    values = [pacer.delay() for _ in range(200)]
    assert slept == values
    assert all(2.0 <= v <= 5.0 for v in values)
    assert len(set(values)) > 150  # genuinely random, not a constant


def test_pacer_is_deterministic_with_a_seeded_rng() -> None:
    a = Pacer(1, 3, random.Random(42), lambda _s: None)
    b = Pacer(1, 3, random.Random(42), lambda _s: None)
    assert [a.delay() for _ in range(5)] == [b.delay() for _ in range(5)]


def test_pacer_rejects_inverted_or_negative_bounds() -> None:
    with pytest.raises(ValueError):
        Pacer(5, 1, random.Random(), lambda _s: None)
    with pytest.raises(ValueError):
        Pacer(-1, 1, random.Random(), lambda _s: None)


def test_pacer_factories_read_action_and_keyword_ranges_from_settings() -> None:
    settings = Settings(
        scrape_delay_min_s=3, scrape_delay_max_s=9, scrape_keyword_pause_min_s=20, scrape_keyword_pause_max_s=60
    )
    actions = Pacer.for_actions(settings, random.Random(1), sleep=lambda _s: None)
    keywords = Pacer.for_keywords(settings, random.Random(1), sleep=lambda _s: None)
    assert (actions.min_s, actions.max_s) == (3, 9)
    assert (keywords.min_s, keywords.max_s) == (20, 60)


def test_short_pause_is_a_quarter_of_the_action_delay_range_from_the_same_rng_and_sleep() -> None:
    slept: list[float] = []
    pacer = Pacer(2.0, 8.0, random.Random(11), slept.append)
    values = [pacer.short() for _ in range(200)]
    assert slept == values
    assert all(0.5 <= v <= 2.0 for v in values)
    assert len(set(values)) > 150
    twin = Pacer(2.0, 8.0, random.Random(11), lambda _s: None)
    assert [twin.short() for _ in range(5)] == values[:5]  # deterministic with a seeded rng


def test_short_pause_of_a_zero_range_pacer_is_zero() -> None:
    slept: list[float] = []
    assert Pacer(0, 0, random.Random(1), slept.append).short() == 0
    assert slept == [0]
