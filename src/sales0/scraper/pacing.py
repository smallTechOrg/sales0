"""Human-like random pacing. Randomness and sleep are injected so tests stay deterministic."""

from __future__ import annotations

import math
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

    def jitter_ms(self, base_ms: float) -> int:
        """``base_ms`` randomised to 60%..160% (never the same wait twice)."""
        return max(1, int(base_ms * self._rng.uniform(0.6, 1.6)))

    def scroll_steps(self, total: int, compact: bool = False) -> list[tuple[int, int]]:
        """A human-ish scroll: ``total`` px split into 3-6 wheel steps as ``(delta_px, wait_ms_after)``.

        Step sizes follow a half-sine (slow start, faster middle, slow end) with per-step noise; the waits
        between steps are random 25-150 ms (longer near the ends). ``compact`` (fixture mode only): exactly 3
        steps, 8-25 ms apart, so the whole burst stays inside the fixture page's ~200 ms one-gesture debounce.. Deltas sum to ``total`` (within rounding).
        """
        total = max(1, int(total))
        n = 3 if compact else self._rng.randint(3, 6)
        weights = [math.sin(math.pi * (i + 0.5) / n) * self._rng.uniform(0.8, 1.2) for i in range(n)]
        scale = total / sum(weights)
        deltas = [max(1, round(w * scale)) for w in weights]
        deltas[n // 2] += total - sum(deltas)  # absorb the rounding in the (largest) middle step
        steps: list[tuple[int, int]] = []
        for i, delta in enumerate(deltas):
            edge = 1.0 + 0.6 * abs(2 * (i + 0.5) / n - 1)  # waits are longer at the start/end of the gesture
            if compact:
                steps.append((delta, self._rng.randint(8, 25)))
            else:
                steps.append((delta, max(1, int(self._rng.uniform(25, 80) * edge) + self._rng.randint(0, 20))))
        return steps

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
