"""Code-enforced rules for suggestions (the model proposes, these decide). Pure functions."""

from __future__ import annotations

import re

from sales0.services.keyword_yield import KeywordYield

DEFAULT_MIN_SCANNED = 30
_TOKEN = re.compile(r"[a-z0-9]{3,}")
_STOP = frozenset(
    "the and for with that this from have has are was were you your our their need needs needed looking look want wants "
    "seeking seek required require requirement hiring hire help best good new top team company india indian near".split()
)


def norm_phrase(text: str) -> str:
    return " ".join((text or "").split()).lower()


def terms(text: str) -> set[str]:
    """Meaningful lowercase terms of ``text`` (stop words dropped, plural 's' folded)."""
    out = set()
    for tok in _TOKEN.findall((text or "").lower()):
        if tok in _STOP:
            continue
        out.add(tok[:-1] if len(tok) > 4 and tok.endswith("s") else tok)
    return out


def is_grounded(phrase: str, evidence: list[int], texts: list[str]) -> str | None:
    """None if ``phrase`` is grounded in the (1-based) evidence indexes, else the reason it is not."""
    if not evidence:
        return "no evidence"
    if any(not 1 <= i <= len(texts) for i in evidence):
        return "evidence index out of range"
    want = terms(phrase)
    if not want:
        return "phrase has no meaningful term"
    if not any(want & terms(texts[i - 1]) for i in set(evidence)):
        return "no evidence enquiry shares a term with the phrase"
    return None


def dead_keywords(
    yields: list[KeywordYield], min_scanned: int = DEFAULT_MIN_SCANNED
) -> list[KeywordYield]:
    """Active keywords that scanned at least ``min_scanned`` posts and found no genuine enquiry."""
    return [
        y
        for y in yields
        if y.keyword_id is not None
        and y.active
        and y.scanned >= min_scanned
        and y.relevant == 0
    ]
