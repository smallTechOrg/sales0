from datetime import date

import pytest

from sales0.scraper.relative_time import parse_relative

TODAY = date(2026, 10, 27)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("5h", date(2026, 10, 27)),
        ("30m", date(2026, 10, 27)),
        ("2d •", date(2026, 10, 25)),
        ("1w • Edited •", date(2026, 10, 20)),
        ("3mo", date(2026, 7, 29)),
        ("1yr", date(2025, 10, 27)),
        ("  2 d  ", date(2026, 10, 25)),
        ("now", date(2026, 10, 27)),
        ("Just now", date(2026, 10, 27)),
        ("12s", date(2026, 10, 27)),
    ],
)
def test_parse_relative_maps_linkedin_suffixes_to_a_date_estimate(raw: str, expected: date) -> None:
    assert parse_relative(raw, TODAY) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "•", "yesterday-ish", "5x", "Promoted"])
def test_parse_relative_returns_none_for_unparseable_text(raw: str | None) -> None:
    assert parse_relative(raw, TODAY) is None
