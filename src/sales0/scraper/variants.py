"""Phase 1.5 search diversification: the passes ``LinkedInSearcher.scan`` runs per phrase.

LinkedIn returns only ~10 cards per (query, date filter, sort). To see more distinct posts per
phrase, a scan runs several PASSES, each a different (query variant, date filter, sort):

1. the baseline passes (the phrase itself, one per configured date filter, configured sort), always first;
2. then variants in a rotation that starts at a different offset each day (and per phrase), so
   repeated runs explore different slices: the other sort, a quote-wrapped exact match, a curated hashtag
   (``#aitrainer``), ``urgent`` / ``requirement`` / ``enquiry`` suffixes, wider/narrower date filters.

The plan is capped at ``scrape_max_passes_per_keyword``; ``scan`` still stops early once the phrase's post
cap is met. Pure and deterministic (no I/O), so it is unit-testable.
"""

from __future__ import annotations

import re
import threading
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


# Curated hashtag map: (pattern on the lower-cased phrase, tag), first match wins. A hashtag is only
# worth a search pass when real people use it, so no mechanical "first two words" tags (#lookingfor).
_HASHTAGS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pat), tag)
    for pat, tag in (
        (r"claude code", "#claudecode"),
        (r"github copilot|copilot", "#copilot"),
        (r"langchain", "#langchain"),
        (r"prompt engineering", "#promptengineering"),
        (r"agentic|ai agent", "#agenticai"),
        (r"chatgpt", "#chatgpt"),
        (r"\bllm\b", "#llm"),
        (r"\brag\b", "#rag"),
        (r"genai|generative ai", "#generativeai"),
        (r"ai literacy", "#ailiteracy"),
        (r"ai upskilling", "#aiupskilling"),
        (r"ai automation", "#aiautomation"),
        (r"ai consult", "#aiconsulting"),
        (r"chatbot", "#chatbot"),
        (r"ai trainer", "#aitrainer"),
        (r"ai training|ai workshop", "#aitraining"),
    )
)
MAX_QUOTED_WORDS = 4


def hashtag_form(phrase: str) -> str | None:
    """A curated hashtag for the phrase (``"Claude Code trainer"`` -> ``"#claudecode"``) or None."""
    low = " ".join(phrase.lower().split())
    for pattern, tag in _HASHTAGS:
        if pattern.search(low):
            return tag
    return None


def quoted_form(phrase: str) -> str | None:
    """The exact-match form; phrases of more than 4 words are not quoted (too rigid to match anything)."""
    clean = " ".join(phrase.replace('"', " ").split())
    n = len(clean.split())
    return f'"{clean}"' if 2 <= n <= MAX_QUOTED_WORDS else None


# Variant queries already planned today, by (day, query, date filter, sort) -> the phrase that owns
# them, so two phrases that map to the same hashtag/suffix query do not both spend a search on it.
# Baseline passes are never deduped, and re-planning the owning phrase is stable.
_claims: dict[tuple[date, str, str, str], str] = {}
_claims_lock = threading.Lock()


def reset_day_claims() -> None:
    with _claims_lock:
        _claims.clear()


def _claim(day: date, p: SearchPass, owner: str) -> bool:
    key = (day, p.query.lower(), p.date_filter, p.sort)
    with _claims_lock:
        for k in [k for k in _claims if k[0] != day]:
            del _claims[k]
        return _claims.setdefault(key, owner) == owner


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
        if key not in seen and _claim(today, p, low):
            seen.add(key)
            out.append(p)
    return out[: max(max_passes, 1)]
