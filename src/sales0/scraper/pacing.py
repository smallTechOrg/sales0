"""Human-like random pacing. Randomness and sleep are injected so tests stay deterministic."""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from sales0.config import Settings


class Pacer:
    def __init__(
        self,
        min_s: float,
        max_s: float,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if min_s < 0 or max_s < 0:
            raise ValueError("pacing bounds must be >= 0")
        if min_s > max_s:
            raise ValueError("pacing min must be <= max")
        self.min_s = min_s
        self.max_s = max_s
        self._rng = rng or random.Random()
        self._sleep = sleep

    def delay(self) -> float:
        """Sleep a random duration in [min_s, max_s]; return it."""
        seconds = self._rng.uniform(self.min_s, self.max_s)
        self._sleep(seconds)
        return seconds

    def short(self) -> float:
        """A shorter pause (Phase 1.2, menu steps): uniform in [min_s/4, max_s/4]; return it."""
        seconds = self._rng.uniform(self.min_s / 4, self.max_s / 4)
        self._sleep(seconds)
        return seconds

    def randint(self, low: int, high: int) -> int:
        """A random int from the same RNG (scroll distances etc.)."""
        return self._rng.randint(low, high)

    @classmethod
    def for_actions(
        cls, settings: Settings, rng: random.Random | None = None, sleep: Callable[[float], None] = time.sleep
    ) -> Pacer:
        """Delay between page actions (navigate, scroll, expand)."""
        return cls(settings.scrape_delay_min_s, settings.scrape_delay_max_s, rng, sleep)

    @classmethod
    def for_keywords(
        cls, settings: Settings, rng: random.Random | None = None, sleep: Callable[[float], None] = time.sleep
    ) -> Pacer:
        """Longer pause between keywords (used by the discovery job)."""
        return cls(settings.scrape_keyword_pause_min_s, settings.scrape_keyword_pause_max_s, rng, sleep)
