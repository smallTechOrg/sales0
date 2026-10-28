"""Pure helpers of the sheet -> CRM mapping (no database)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sales0.services.sheetsync import SheetMetricRow, SheetMetricWeek
from sales0.services.sheetsync.mapping import Writer, first, jsonify, json_fields, key_text, local_midnight_utc, overlay


def test_jsonify_dates_tuples_and_week_dataclasses() -> None:
    row = SheetMetricRow(key="k", metric_set="weekly", metric="m", weeks=(SheetMetricWeek("Week 1", 1, 1, "3", 3.0),))
    f = json_fields(row)
    assert f["weeks"] == [{"label": "Week 1", "week_no": 1, "occurrence": 1, "raw": "3", "value": 3.0}]
    assert jsonify((date(2026, 7, 9), None, ("a",))) == ["2026-07-09", None, ["a"]]


def test_overlay_later_tab_wins_only_where_non_none() -> None:
    merged = overlay([{"status": "Message sent", "name": "A", "x": None}, {"status": None, "name": "B", "x": None}])
    assert merged == {"status": "Message sent", "name": "B", "x": None}


@dataclass
class Row:
    city: str | None = None
    email: str | None = None
    n: int | None = None


def test_writer_sets_clears_and_leaves_alone() -> None:
    row = Row(city="Pune", email="a@x.example")
    w = Writer(row, old={"city": "Pune", "email": "a@x.example", "n": None}, new={"city": "Goa", "email": None, "n": None})
    w.pull("city", None)
    w.pull("email", None)  # was non-null, now null: cleared
    w.pull("n", None)  # null -> null: untouched
    assert (row.city, row.email, row.n) == ("Goa", None, None)
    assert w.changed == ["city", "email"]
    w2 = Writer(row, old={}, new={"n": 3})
    w2.pull("n", None, int)
    assert row.n == 3 and w2.changed == ["n"]
    w2.pull("n", None, int)  # same value: no second change entry
    assert w2.changed == ["n"]


def test_small_helpers() -> None:
    assert key_text("  Hello   World ") == "hello world" and first(["a", "b"]) == "a" and first([]) is None
    assert local_midnight_utc(None) is None
    d = local_midnight_utc(date(2026, 7, 9))
    assert d is not None and d.tzinfo == UTC and d.date() in (date(2026, 7, 8), date(2026, 7, 9))
    assert isinstance(d, datetime)
