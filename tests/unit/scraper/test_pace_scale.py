"""SALES0_SCRAPE_PACE_SCALE: the fixture-only speed knob and its live-mode guard."""

from __future__ import annotations

import random
from typing import Any

import pytest
from pydantic import ValidationError

from sales0.config import Settings
from sales0.linkedin_fixture.corpus import LAZY_REVEAL_DELAY_MS
from sales0.scraper import browser as browser_module
from sales0.scraper import search as search_module
from sales0.scraper.pacing import Pacer, pace_scale, scaled_ms


def _s(tmp_path: Any, **over: Any) -> Settings:
    return Settings(data_dir=str(tmp_path), **over)


def test_default_is_exactly_one_and_unscaled(tmp_path: Any) -> None:
    for mode, settle in (("fixture", 1_200), ("live", 2_500)):
        s = _s(tmp_path, linkedin_mode=mode)
        assert s.scrape_pace_scale == 1.0 and pace_scale(s) == 1.0
        assert search_module.scroll_settle_ms(s) == settle == browser_module.scroll_settle_ms(s)
        assert search_module.poll_ms(s) == 250
        p = Pacer.for_actions(s, random.Random(3), lambda _s: None)
        assert (p.min_s, p.max_s, p.scale) == (s.scrape_delay_min_s, s.scrape_delay_max_s, 1.0)
        assert p.jitter_ms(700) == Pacer(3.0, 9.0, random.Random(3)).jitter_ms(700)


@pytest.mark.parametrize("value", ["0.5", "0.05", "0.99"])
def test_live_mode_rejects_anything_below_one(tmp_path: Any, value: str) -> None:
    with pytest.raises(ValidationError, match="fixture"):
        _s(tmp_path, linkedin_mode="live", scrape_pace_scale=float(value))


def test_live_mode_rejects_it_from_the_environment(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_PACE_SCALE", "0.1")
    monkeypatch.setenv("SALES0_LINKEDIN_MODE", "live")
    with pytest.raises(ValidationError):
        Settings(data_dir=str(tmp_path))


def test_live_mode_can_never_go_below_one_even_if_validation_is_bypassed(tmp_path: Any) -> None:
    s = _s(tmp_path, linkedin_mode="live")
    object.__setattr__(s, "scrape_pace_scale", 0.1)  # a hand-built / mutated Settings
    assert pace_scale(s) == 1.0
    assert search_module.scroll_settle_ms(s) == 2_500
    assert Pacer.for_actions(s).min_s == s.scrape_delay_min_s


def test_fixture_mode_clamps_to_range(tmp_path: Any) -> None:
    assert pace_scale(_s(tmp_path, linkedin_mode="fixture", scrape_pace_scale=0.2)) == 0.2
    assert pace_scale(_s(tmp_path, linkedin_mode="fixture", scrape_pace_scale=0.001)) == 0.05
    assert pace_scale(_s(tmp_path, linkedin_mode="fixture", scrape_pace_scale=7)) == 1.0


def test_scaling_moves_every_wait_together_and_keeps_ratios(tmp_path: Any) -> None:
    s = _s(tmp_path, linkedin_mode="fixture", scrape_pace_scale=0.25)
    assert search_module.scroll_settle_ms(s) == 300 == browser_module.scroll_settle_ms(s)
    assert search_module.poll_ms(s) == 62
    assert scaled_ms(s, LAZY_REVEAL_DELAY_MS) == 175
    # the lazy-reveal delay stays shorter than the settle window at every scale
    for k in (0.05, 0.1, 0.25, 0.5, 1.0):
        sk = _s(tmp_path, linkedin_mode="fixture", scrape_pace_scale=k)
        assert scaled_ms(sk, LAZY_REVEAL_DELAY_MS) < search_module.scroll_settle_ms(sk)
        assert search_module.poll_ms(sk) < search_module.scroll_settle_ms(sk)
    p = Pacer.for_actions(s)
    assert (p.min_s, p.max_s) == (s.scrape_delay_min_s * 0.25, s.scrape_delay_max_s * 0.25)
    k = Pacer.for_keywords(s)
    assert k.max_s == s.scrape_keyword_pause_max_s * 0.25
    seeds = range(20)
    full = [Pacer(3, 9, random.Random(i)).jitter_ms(700) for i in seeds]
    quarter = [Pacer(3, 9, random.Random(i), scale=0.25).jitter_ms(700) for i in seeds]
    assert all(abs(q - f * 0.25) <= 1 for f, q in zip(full, quarter))
