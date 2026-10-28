"""Phase 1.5 search diversification: the passes ``LinkedInSearcher.scan`` runs per phrase.

LinkedIn returns only ~10 cards per (query, date filter, sort). To see more distinct posts per
phrase, a scan runs several PASSES, each a different (query variant, date filter, sort):

1. the baseline passes (the phrase itself, one per configured date filter, configured sort), always first;
2. then variants in a rotation that starts at a different offset each day (and per phrase), so
   repeated runs explore different slices: the other sort, a quote-wrapped exact match, a hashtag
   form (``#aitrainer``), ``urgent`` / ``requirement`` / ``enquiry`` suffixes, wider/narrower date filters.

The plan is capped at ``scrape_max_passes_per_keyword``; ``scan`` still stops early once the phrase's post
cap is met. Pure and deterministic (no I/O), so it is unit-testable.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from datetime import date
from typing import Literal

Sort = Literal["latest", "relevance"]
SUFFIXES = ("urgent", "requirement", "enquiry")
_WORD = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class SearchPass:
    query: str
    date_filter: str
    sort: Sort
    kind: str  # baseline | other_sort | quoted | hashtag | suffix:<word> | wider | narrower


def hashtag_form(phrase: str) -> str | None:
    """``"ai trainer requirement"`` -> ``"#aitrainer"`` (first two words, ASCII only); None when too short."""
    words = _WORD.findall(phrase)
    if len(words) < 2:
        return None
    tag = "".join(words[:2]).lower()
    return f"#{tag}" if len(tag) >= 4 else None


def quoted_form(phrase: str) -> str | None:
    clean = " ".join(phrase.replace('"', " ").split())
    return f'"{clean}"' if len(clean.split()) >= 2 else None


def plan_passes(
    phrase: str,
    date_filters: tuple[str, ...],
    sort: Sort,
    *,
    variants: bool,
    max_passes: int,
    today: date,
) -> list[SearchPass]:
    """The ordered passes for ``phrase`` (at most ``max_passes``, at least the baseline ones that fit)."""
    phrase = " ".join(phrase.split())
    filters = date_filters or ("past-month",)
    baseline = [SearchPass(phrase, f, sort, "baseline") for f in filters]
    if not variants:
        return baseline
    other: Sort = "relevance" if sort == "latest" else "latest"
    wide = "past-month"
    mid = "past-week"
    low = phrase.lower()
    extras: list[SearchPass] = [SearchPass(phrase, wide, other, "other_sort")]
    quoted, tag = quoted_form(phrase), hashtag_form(phrase)
    if quoted:
        extras.append(SearchPass(quoted, wide, sort, "quoted"))
    if tag:
        extras.append(SearchPass(tag, wide, sort, "hashtag"))
    for word in SUFFIXES:
        if word not in low:
            extras.append(SearchPass(f"{phrase} {word}", mid, sort, f"suffix:{word}"))
    extras.append(SearchPass(phrase, "past-24h", sort, "narrower"))
    if quoted:
        extras.append(SearchPass(quoted, mid, other, "quoted_other_sort"))
    # Rotate so each day (and each phrase) starts the variant list somewhere else.
    if extras:
        offset = (today.toordinal() + zlib.crc32(low.encode())) % len(extras)
        extras = extras[offset:] + extras[:offset]
    seen = {(p.query, p.date_filter, p.sort) for p in baseline}
    out = list(baseline)
    for p in extras:
        key = (p.query, p.date_filter, p.sort)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out[: max(max_passes, 1)]
