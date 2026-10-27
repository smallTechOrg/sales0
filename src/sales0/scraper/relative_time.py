"""LinkedIn relative timestamps ("5h", "2d •", "1w • Edited", "3mo", "1yr") → a date estimate."""

from __future__ import annotations

import re
from datetime import date, timedelta

_PATTERN = re.compile(r"^\s*(\d+)\s*(s|m|min|h|d|w|mo|y|yr)\b", re.IGNORECASE)
_NOW_WORDS = ("now", "just now")
_DAYS = {"s": 0, "m": 0, "min": 0, "h": 0, "d": 1, "w": 7, "mo": 30, "y": 365, "yr": 365}


def normalize_relative(raw: str | None) -> str | None:
    """'2d • Edited •' → '2d'. Returns None when nothing usable is left."""
    if raw is None:
        return None
    head = raw.split("•", 1)[0].strip()
    return head or None


def parse_relative(raw: str | None, today: date) -> date | None:
    head = normalize_relative(raw)
    if head is None:
        return None
    if head.lower() in _NOW_WORDS:
        return today
    match = _PATTERN.match(head)
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2).lower()
    return today - timedelta(days=amount * _DAYS[unit])
