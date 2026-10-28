"""Pure cell-level parsers: nulls, dates without a year, emails, phones, URLs, mojibake.

Everything here is deterministic, side-effect free and never raises on bad input
(unparseable -> ``None``); callers decide whether that is a warning.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from urllib.parse import urlsplit

NULL_TOKENS = frozenset(
    {
        "",
        "-",
        "--",
        "–",
        "—",
        "n/a",
        "na",
        "n.a.",
        "none",
        "null",
        "nil",
        "not provided",
        "not available",
        "not applicable",
        "not given",
        "tbd",
        "?",
    }
)

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MOJIBAKE_HINT = re.compile("[ÃÂâð][\u0080-¿ŒœŠšŸŽžƒˆ˜–-›€™]")


def fix_mojibake(text: str) -> str:
    """Repair UTF-8 text that was decoded as cp1252/latin-1 (e.g. ``â€™`` -> ``’``)."""
    if not _MOJIBAKE_HINT.search(text):
        return text
    for codec in ("cp1252", "latin-1"):
        try:
            fixed = text.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        return fixed
    return text


def clean_text(raw: str | None, *, max_chars: int = 20000) -> tuple[str, bool]:
    """Normalise one cell: repair mojibake, unify newlines, drop control chars, strip, cap.

    Returns ``(text, truncated)``.
    """
    if not raw:
        return "", False
    text = fix_mojibake(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = _CONTROL.sub("", text).strip()
    if len(text) > max_chars:
        return text[:max_chars].rstrip(), True
    return text, False


def is_null(text: str | None) -> bool:
    return text is None or text.strip().strip(".").strip().lower() in NULL_TOKENS


def nullable(text: str | None) -> str | None:
    """``None`` for blank / '-' / 'Not provided' style cells, else the stripped text."""
    return None if is_null(text) else (text or "").strip()


def strip_markdown_wrap(text: str) -> str:
    """``**Title**`` -> ``Title`` (the sheet stores some summaries bold-wrapped)."""
    t = text.strip()
    while len(t) > 4 and t.startswith("**") and t.endswith("**"):
        t = t[2:-2].strip()
    return t


# ----------------------------------------------------------------------------- dates
MONTHS: dict[str, int] = {}
for _i, _names in enumerate(
    [
        ("jan", "january"),
        ("feb", "february"),
        ("mar", "march"),
        ("apr", "april"),
        ("may",),
        ("jun", "june"),
        ("jul", "july"),
        ("aug", "august"),
        ("sep", "sept", "september"),
        ("oct", "october"),
        ("nov", "november"),
        ("dec", "december"),
    ],
    start=1,
):
    for _n in _names:
        MONTHS[_n] = _i

_DAY_MON = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?[\s\-/.,]+([A-Za-z]{3,9})\.?(?:[\s\-/.,]+(\d{2,4}))?$")
_MON_DAY = re.compile(r"^([A-Za-z]{3,9})\.?[\s\-/.,]+(\d{1,2})(?:st|nd|rd|th)?(?:[\s\-/.,]+(\d{2,4}))?$")
_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T\s].*)?$")
_NUMERIC = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2,4}))?$")


def _year(y: str) -> int:
    n = int(y)
    return 2000 + n if n < 100 else n


def _safe(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _nearest_past(month: int, day: int, ref: date) -> date | None:
    """Latest valid ``month/day`` that is <= ``ref`` (walks back over leap-day gaps)."""
    for y in range(ref.year, ref.year - 9, -1):
        cand = _safe(y, month, day)
        if cand is not None and cand <= ref:
            return cand
    return None


def parse_date(text: str | None, *, ref: date, not_after: date | None = None) -> date | None:
    """Parse a sheet date; a missing year is inferred as the nearest past occurrence.

    ``ref`` is "today" (injected). ``not_after`` (e.g. Date Added when parsing Date Posted)
    caps the inferred year so a post is never dated after it was added. Numeric forms are
    day-first (Indian sheets): ``09/07/2026`` is 9 July. Explicit years are kept as written.
    """
    t = nullable(text)
    if t is None:
        return None
    t = t.strip().strip(",.")
    limit = ref if not_after is None else min(ref, not_after)

    if m := _ISO.match(t):
        return _safe(int(m[1]), int(m[2]), int(m[3]))
    if m := _NUMERIC.match(t):
        day, mon = int(m[1]), int(m[2])
        if m[3]:
            return _safe(_year(m[3]), mon, day)
        return _nearest_past(mon, day, limit) if 1 <= mon <= 12 else None
    for pattern, day_first in ((_DAY_MON, True), (_MON_DAY, False)):
        if m := pattern.match(t):
            name, dd, yy = (m[2], m[1], m[3]) if day_first else (m[1], m[2], m[3])
            mon = MONTHS.get(name.lower())
            if mon is None:
                continue
            if yy:
                return _safe(_year(yy), mon, int(dd))
            return _nearest_past(mon, int(dd), limit)
    return None


# ----------------------------------------------------------------------------- emails
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")


def parse_emails(text: str | None) -> tuple[str, ...]:
    """Every distinct e-mail address in a free-text cell, lower-cased, in order."""
    if not text:
        return ()
    seen: dict[str, None] = {}
    for m in _EMAIL.finditer(text):
        seen.setdefault(m.group(0).strip(".").lower(), None)
    return tuple(seen)


# ----------------------------------------------------------------------------- phones
_PHONE_CANDIDATE = re.compile(r"\+?\d[\d\s\-().]{5,}\d")


def normalise_phone(candidate: str) -> str | None:
    """One phone number -> ``+<digits>`` (10-digit / 0-prefixed / 91-prefixed => India).

    Without a ``+`` and an unknown shape the bare digits are returned only when plausible
    (8-15 digits); anything else is ``None``.
    """
    plus = candidate.strip().startswith("+")
    digits = re.sub(r"\D", "", candidate)
    if plus:
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    if len(digits) == 11 and digits[0] == "0" and digits[1] in "6789":
        return f"+91{digits[1:]}"
    if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return f"+{digits}"
    if 8 <= len(digits) <= 15:
        return digits
    return None


def parse_phones(text: str | None) -> tuple[str, ...]:
    """All phone numbers in a cell (spaces/dashes/brackets inside numbers tolerated)."""
    if is_null(text):
        return ()
    seen: dict[str, None] = {}
    # Split on separators that cannot be inside a number first.
    for chunk in re.split(r"[/;,|\n]|\band\b|\bor\b", text or "", flags=re.IGNORECASE):
        for m in _PHONE_CANDIDATE.finditer(chunk):
            norm = normalise_phone(m.group(0))
            if norm:
                seen.setdefault(norm, None)
    return tuple(seen)


# ----------------------------------------------------------------------------- urls / slugs
_URL_LIKE = re.compile(r"^(?:https?://|www\.)", re.IGNORECASE)
_ACTIVITY = re.compile(r"(?:activity[-:]|ugcPost[-:]|share[-:])(\d{6,})", re.IGNORECASE)
_LI_SLUG = re.compile(r"linkedin\.com/(?:in|company|pub)/([^/?#\s]+)", re.IGNORECASE)
_TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|trk|trackingid|originalsubdomain)", re.IGNORECASE)


def looks_like_url(text: str | None) -> bool:
    return bool(text and _URL_LIKE.match(text.strip()))


def normalize_url(text: str | None) -> str | None:
    """Stable URL form: scheme/host lower-cased, ``www.`` and tracking params dropped."""
    t = nullable(text)
    if t is None:
        return None
    t = t.split()[0]
    if not looks_like_url(t):
        return None
    if t.lower().startswith("www."):
        t = "https://" + t
    try:
        parts = urlsplit(t)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return None
    path = parts.path.rstrip("/")
    query = "&".join(sorted(p for p in parts.query.split("&") if p and not _TRACKING.match(p)))
    out = f"{host}{path}"
    return f"{out}?{query}" if query else out


def post_key(url: str | None) -> str | None:
    """Stable key for a social post link: the numeric activity id when present."""
    t = nullable(url)
    if t is None or not looks_like_url(t):
        return None
    m = _ACTIVITY.search(t)
    if m:
        return f"post:{m.group(1)}"
    norm = normalize_url(t)
    return f"url:{norm}" if norm else None


def linkedin_slug(url: str | None) -> str | None:
    """``https://www.linkedin.com/in/some-name-123/?x=1`` -> ``some-name-123`` (lower-cased)."""
    t = nullable(url)
    if t is None:
        return None
    m = _LI_SLUG.search(t)
    if m:
        return m.group(1).lower()
    return None


def website_key(text: str | None) -> str | None:
    """Normalised website host(+path) for competitor keys; bare ``example.com`` accepted."""
    t = nullable(text)
    if t is None:
        return None
    t = t.split()[0]
    if "." not in t or "@" in t:
        return None
    if not looks_like_url(t):
        t = "https://" + t
    return normalize_url(t)


# ----------------------------------------------------------------------------- hashing
def stable_hash(*parts: str, length: int = 24) -> str:
    h = hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()
    return h[:length]


def norm_key_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())
