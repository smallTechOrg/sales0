"""A tiny fake clock for tests that inject ``now=`` / ``sleep=`` callables."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now

    def sleep(self, seconds: float) -> None:
        """Records the request and advances time; never blocks."""
        self.sleeps.append(float(seconds))
        self._now += timedelta(seconds=seconds)

    def monotonic(self) -> float:
        return self._now.timestamp()
