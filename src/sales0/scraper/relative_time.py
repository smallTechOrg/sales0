"""LinkedIn relative timestamps → a date estimate.

Accepts the short form ("5h", "2d •", "1w • Edited •", "3mo", "1yr", "5 mins"), the long
form ("2 days ago", "1 week ago"), "now"/"Just now", and an ISO date/datetime (the
``datetime`` attribute of a ``<time>`` element). A leading "Edited •" and trailing "•"
are stripped.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from sales0.scraper.selectors import RELATIVE_TIME_LONG_RE, RELATIVE_TIME_SHORT_RE

SHORT_RE = re.compile(RELATIVE_TIME_SHORT_RE, re.IGNORECASE)
LONG_RE = re.compile(RELATIVE_TIME_LONG_RE, re.IGNORECASE)
_EDITED_PREFIX = re.compile(r"^\s*edited\s*[•·]\s*", re.IGNORECASE)
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_NOW_WORDS = ("now", "just now")
_SHORT_DAYS = {
    "s": 0, "m": 0, "min": 0, "mins": 0, "h": 0, "hr": 0, "hrs": 0,
    "d": 1, "w": 7, "wk": 7, "mo": 30, "y": 365, "yr": 365, "yrs": 365,
}
_LONG_DAYS = {"second": 0, "minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}


def normalize_relative(raw: str | None) -> str | None:
    """'2d • Edited •' → '2d'; 'Edited • 3h' → '3h'. Returns None when nothing usable is left."""
    if raw is None:
        return None
    text = " ".join(raw.split())
    text = _EDITED_PREFIX.sub("", text)
    head = text.split("•", 1)[0].strip()
    return head or None


def is_relative_time(raw: str | None) -> bool:
    head = normalize_relative(raw)
    if head is None:
        return False
    return head.lower() in _NOW_WORDS or bool(SHORT_RE.match(head) or LONG_RE.search(head))


def parse_relative(raw: str | None, today: date) -> date | None:
    head = normalize_relative(raw)
    if head is None:
        return None
    if head.lower() in _NOW_WORDS:
        return today
    if _ISO_RE.match(head):
        try:
            return datetime.fromisoformat(head.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    match = SHORT_RE.match(head)
    if match:
        return today - timedelta(days=int(match.group(1)) * _SHORT_DAYS[match.group(2).lower()])
    match = LONG_RE.search(head)
    if match:
        return today - timedelta(days=int(match.group(1)) * _LONG_DAYS[match.group(2).lower()])
    return None
