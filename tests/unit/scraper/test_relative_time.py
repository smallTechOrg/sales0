from datetime import date

import pytest

from sales0.scraper.relative_time import is_relative_time, normalize_relative, parse_relative

TODAY = date(2026, 10, 27)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("5h", date(2026, 10, 27)),
        ("30m", date(2026, 10, 27)),
        ("12 mins", date(2026, 10, 27)),
        ("2d •", date(2026, 10, 25)),
        ("1w • Edited •", date(2026, 10, 20)),
        ("Edited • 2d", date(2026, 10, 25)),
        ("3wk", date(2026, 10, 6)),
        ("3mo", date(2026, 7, 29)),
        ("1yr", date(2025, 10, 27)),
        ("2yrs", date(2024, 10, 27)),
        ("  2 d  ", date(2026, 10, 25)),
        ("4hr", date(2026, 10, 27)),
        ("now", date(2026, 10, 27)),
        ("Just now", date(2026, 10, 27)),
        ("12s", date(2026, 10, 27)),
    ],
)
def test_parse_relative_maps_linkedin_short_forms_to_a_date_estimate(raw: str, expected: date) -> None:
    assert parse_relative(raw, TODAY) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2 days ago", date(2026, 10, 25)),
        ("1 day ago", date(2026, 10, 26)),
        ("3 hours ago", date(2026, 10, 27)),
        ("1 week ago", date(2026, 10, 20)),
        ("2 Months ago", date(2026, 8, 28)),
        ("1 year ago", date(2025, 10, 27)),
        ("Posted 4 days ago", date(2026, 10, 23)),
    ],
)
def test_parse_relative_accepts_the_long_ago_form(raw: str, expected: date) -> None:
    assert parse_relative(raw, TODAY) == expected


def test_parse_relative_accepts_an_iso_datetime_attribute() -> None:
    assert parse_relative("2026-10-22", TODAY) == date(2026, 10, 22)
    assert parse_relative("2026-10-22T08:15:00Z", TODAY) == date(2026, 10, 22)
    assert parse_relative("2026-13-40", TODAY) is None


@pytest.mark.parametrize("raw", [None, "", "   ", "•", "yesterday-ish", "5x", "Promoted", "2nd", "3rd+", "1st", "40 managers"])
def test_parse_relative_returns_none_for_unparseable_text(raw: str | None) -> None:
    assert parse_relative(raw, TODAY) is None


def test_normalize_relative_strips_bullets_and_the_edited_marker() -> None:
    assert normalize_relative("1d • Edited •") == "1d"
    assert normalize_relative("Edited • 3h •") == "3h"
    assert normalize_relative("  • ") is None
    assert normalize_relative(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("2d •", True), ("2 days ago", True), ("now", True), ("• 2nd", False), ("Head of L&D", False), (None, False)],
)
def test_is_relative_time_recognises_only_time_labels(raw: str | None, expected: bool) -> None:
    assert is_relative_time(raw) is expected


# --------------------------------------------------------------------------- Phase 1.2 (SDUI actor lines)
@pytest.mark.parametrize(
    ("raw", "head", "expected"),
    [
        ("24m", "24m", date(2026, 10, 27)),
        ("3h", "3h", date(2026, 10, 27)),
        ("2d", "2d", date(2026, 10, 25)),
        ("1w", "1w", date(2026, 10, 20)),
        ("24m • Edited", "24m", date(2026, 10, 27)),
        ("2d • Edited •", "2d", date(2026, 10, 25)),
        ("1w · Edited", "1w", date(2026, 10, 20)),  # middle dot separator
        ("3h ·", "3h", date(2026, 10, 27)),
        ("Edited · 2d", "2d", date(2026, 10, 25)),
    ],
)
def test_sdui_time_lines_with_edited_suffixes_normalise_and_parse(raw: str, head: str, expected: date) -> None:
    assert normalize_relative(raw) == head
    assert is_relative_time(raw) is True
    assert parse_relative(normalize_relative(raw), TODAY) == expected


@pytest.mark.parametrize("raw", ["Edited", "• Edited", "Follow", "• 3rd+", "Feed post", "Visible to anyone on or off LinkedIn"])
def test_sdui_non_time_actor_lines_are_not_times(raw: str) -> None:
    assert is_relative_time(raw) is False
    assert parse_relative(raw, TODAY) is None
