"""ISO-week arithmetic in the configured timezone (Monday-start weeks, local-midnight boundaries)."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sales0.clock import ensure_utc, local_today


@dataclass(frozen=True)
class Week:
    iso_year: int
    iso_week: int
    monday: date
    start_utc: datetime  # inclusive
    end_utc: datetime  # exclusive

    @property
    def key(self) -> str:
        return f"{self.iso_year}-W{self.iso_week:02d}"

    @property
    def sunday(self) -> date:
        return self.monday + timedelta(days=6)

    @property
    def label(self) -> str:
        return f"W{self.iso_week:02d}"

    @property
    def range_label(self) -> str:
        return f"{self.monday:%d %b} - {self.sunday:%d %b}"


def _utc_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)


def week_for_date(day: date, tz: ZoneInfo) -> Week:
    iso = day.isocalendar()
    monday = day - timedelta(days=day.weekday())
    return Week(iso.year, iso.week, monday, _utc_midnight(monday, tz), _utc_midnight(monday + timedelta(days=7), tz))


def week_containing(now: datetime, tz: ZoneInfo) -> Week:
    return week_for_date(local_today(tz, now), tz)


def parse_week_key(key: str, tz: ZoneInfo) -> Week | None:
    """``2026-W43`` -> Week (None when malformed or not a real ISO week)."""
    try:
        year_s, week_s = key.strip().upper().split("-W")
        monday = date.fromisocalendar(int(year_s), int(week_s), 1)
    except (ValueError, TypeError):
        return None
    return week_for_date(monday, tz)


def last_weeks(now: datetime, tz: ZoneInfo, n: int) -> list[Week]:
    """The ``n`` weeks ending with the current one, oldest first."""
    cur = week_containing(now, tz)
    return [week_for_date(cur.monday - timedelta(days=7 * i), tz) for i in range(n - 1, -1, -1)]


def weeks_ending(last: Week, tz: ZoneInfo, n: int) -> list[Week]:
    return [week_for_date(last.monday - timedelta(days=7 * i), tz) for i in range(n - 1, -1, -1)]


class Bucketer:
    """Maps a UTC instant to the index of the week containing it (None when outside the range)."""

    def __init__(self, weeks: list[Week]) -> None:
        self.weeks = weeks
        self._starts = [w.start_utc for w in weeks]
        self.lo = weeks[0].start_utc
        self.hi = weeks[-1].end_utc

    def index(self, instant: datetime | None) -> int | None:
        if instant is None:
            return None
        t = ensure_utc(instant)
        if t < self.lo or t >= self.hi:
            return None
        return bisect_right(self._starts, t) - 1

    def count(self, instants: Iterable[datetime | None]) -> list[int]:
        out = [0] * len(self.weeks)
        for t in instants:
            i = self.index(t)
            if i is not None:
                out[i] += 1
        return out

    def distinct(self, pairs: Iterable[tuple[object, datetime | None]]) -> list[int]:
        """Distinct keys per week from ``(key, instant)`` pairs."""
        seen: list[set[object]] = [set() for _ in self.weeks]
        for key, t in pairs:
            i = self.index(t)
            if i is not None:
                seen[i].add(key)
        return [len(s) for s in seen]


__all__ = ["Bucketer", "Week", "last_weeks", "parse_week_key", "week_containing", "week_for_date", "weeks_ending"]
