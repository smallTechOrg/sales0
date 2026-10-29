"""The injectable time seam. All "now" and "today" values come from here."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo


def _zone(tz: str | ZoneInfo) -> ZoneInfo:
    return tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)


def utcnow() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Treat naive datetimes as UTC (SQLite returns naive values) and convert to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def local_now(tz: str | ZoneInfo, now: datetime | None = None) -> datetime:
    return ensure_utc(now or utcnow()).astimezone(_zone(tz))


def local_today(tz: str | ZoneInfo, now: datetime | None = None) -> date:
    """The calendar date in ``tz`` (used for daily caps and "today" totals)."""
    return local_now(tz, now).date()


def local_day_bounds_utc(tz: str | ZoneInfo, now: datetime | None = None) -> tuple[datetime, datetime]:
    """[start, end) of the local day containing ``now``, expressed in UTC."""
    zone = _zone(tz)
    day = local_today(zone, now)
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    end_day = date.fromordinal(day.toordinal() + 1)
    end = datetime(end_day.year, end_day.month, end_day.day, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def local_month_bounds_utc(tz: str | ZoneInfo, now: datetime | None = None) -> tuple[datetime, datetime]:
    """[start, end) of the local month containing ``now``, expressed in UTC."""
    zone = _zone(tz)
    day = local_today(zone, now)
    start = datetime(day.year, day.month, 1, tzinfo=zone)
    end = datetime(day.year + (day.month == 12), day.month % 12 + 1, 1, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)
