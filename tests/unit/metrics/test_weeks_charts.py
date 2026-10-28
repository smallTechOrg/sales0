from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sales0.services.metrics import charts, exports
from sales0.services.metrics.weeks import Bucketer, last_weeks, parse_week_key, week_for_date

IST = ZoneInfo("Asia/Kolkata")


def test_iso_week_year_rollover_and_53_week_years() -> None:
    w = week_for_date(date(2027, 1, 1), IST)  # Friday of ISO week 53 of 2026
    assert (w.iso_year, w.iso_week, w.monday) == (2026, 53, date(2026, 12, 28))
    assert parse_week_key("2026-W53", IST).monday == date(2026, 12, 28)  # type: ignore[union-attr]
    assert parse_week_key("2027-W53", IST) is None and parse_week_key("junk", IST) is None


def test_bucketer_boundaries() -> None:
    weeks = last_weeks(datetime(2026, 10, 28, 6, 30, tzinfo=UTC), IST, 2)
    b = Bucketer(weeks)
    assert b.index(datetime(2026, 10, 18, 18, 29, 59, tzinfo=UTC)) is None  # Sun 23:59:59 IST before the range
    assert b.index(datetime(2026, 10, 18, 18, 30, tzinfo=UTC)) == 0  # Mon 00:00 IST
    assert b.index(datetime(2026, 10, 25, 18, 29, 59, tzinfo=UTC)) == 0
    assert b.index(datetime(2026, 10, 25, 18, 30, tzinfo=UTC)) == 1
    assert b.index(datetime(2026, 11, 1, 18, 30, tzinfo=UTC)) is None
    assert b.distinct([("a", datetime(2026, 10, 20, tzinfo=UTC)), ("a", datetime(2026, 10, 21, tzinfo=UTC))]) == [1, 0]


def test_charts_escape_labels_and_handle_empty_series() -> None:
    svg = str(charts.bar_chart("<b>t</b>", ["W1", "<i>"], [("a&b", [0, None])]))
    assert "<b>" not in svg and "&lt;b&gt;" in svg and "<i>" not in svg
    assert "<svg" in str(charts.line_chart("t", ["W1"], [("a", [None])]))
    assert "<svg" in str(charts.funnel_chart("t", [("x", 0, None)]))


def test_csv_cell_guard() -> None:
    assert exports._safe("=1+1") == "'=1+1" and exports._safe("+x") == "'+x" and exports._safe("@a") == "'@a"
    assert exports._safe("-5") == "-5" and exports._safe("-cmd") == "'-cmd" and exports._safe(None) == ""
