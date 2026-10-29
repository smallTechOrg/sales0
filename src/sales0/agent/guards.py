"""Pure output guardrails applied to every parsed ``TriageResult`` (agent.md -> validate_and_guard).

Anti-hallucination: contact details survive only if they literally appear in the post;
an offering id survives only if it is in the active catalogue.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from sales0.agent.schemas import TriageInput, TriageResult

REASON_MAX_CHARS = 160
SUMMARY_MAX_CHARS = 140
LANGUAGE_MAX_CHARS = 8
PHONE_MIN_DIGITS = 8
ELLIPSIS = "…"
NO_REASON = "No reason given"

_WS = re.compile(r"\s+")
_NON_DIGIT = re.compile(r"\D")
# One written number: digits joined only by the separators phones use (never by letters/newlines).
_DIGIT_RUN = re.compile(r"\d(?:[\d \t\u00a0()\-\u2013/]*\d)?")
_TOKEN = re.compile(r"[\w&]+", re.UNICODE)
_MAX_DIAL_PREFIX = 4  # country code + trunk zero(s) a number may carry in front of the national digits


def one_line(text: str | None, max_chars: int) -> str:
    """Collapse all whitespace to single spaces and truncate with an ellipsis."""
    collapsed = _WS.sub(" ", text or "").strip()
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max_chars - len(ELLIPSIS)].rstrip() + ELLIPSIS


def clean_optional(value: str | None) -> str | None:
    """Strip; an empty string becomes None."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def guard_email(value: str | None, raw_text: str) -> str | None:
    """Lower-cased email, or None unless it appears as a whole address (case-insensitively) in the raw text."""
    email = clean_optional(value)
    if email is None:
        return None
    email = email.lower()
    email = email.removeprefix("mailto:")
    email = email.strip("<>()[]\"' ,;.")
    if "@" not in email:
        return None
    # Bounded match: a fragment of a longer address is rejected — ``rao@x.ex`` from
    # ``arjun.rao@x.ex`` (left bound) and ``a@x`` from ``a@x.example`` (right bound; a dot is
    # only allowed after the address when it ends a sentence, not when a label follows).
    pattern = rf"(?<![\w.+-]){re.escape(email)}(?![\w-]|\.[\w-])"
    if re.search(pattern, raw_text, flags=re.IGNORECASE) is None:
        return None
    return email


def guard_phone(value: str | None, raw_text: str) -> str | None:
    """The phone as written, or None unless its ≥8-digit sequence appears in the raw text's digits."""
    phone = clean_optional(value)
    if phone is None:
        return None
    digits = _NON_DIGIT.sub("", phone)
    if len(digits) < PHONE_MIN_DIGITS:
        return None
    for run in _DIGIT_RUN.finditer(raw_text):
        found = _NON_DIGIT.sub("", run.group(0))
        if len(found) < PHONE_MIN_DIGITS:
            continue
        # The same number, written with or without a country code / trunk zero — never a fragment
        # of a longer digit run and never digits stitched together from different numbers.
        if found == digits:
            return phone
        if abs(len(found) - len(digits)) <= _MAX_DIAL_PREFIX:
            short, long_ = sorted((found, digits), key=len)
            if long_.endswith(short) or (len(short) >= 10 and long_[-10:] == short[-10:]):
                return phone
    return None


def guard_grounded(value: str | None, *haystacks: str | None) -> str | None:
    """A free-text fact (company, person, city, topic) survives only if it is grounded in the post.

    The stripped value must appear case-insensitively in the post text (or the author's name /
    headline), or at least half of its significant tokens must (so "Priya" for "Priya Menon" or
    "ACME Logistics Pvt Ltd" for "Acme Logistics" survive). Otherwise it is dropped (None).
    """
    cleaned = clean_optional(value)
    if cleaned is None:
        return None
    hay = _WS.sub(" ", " ".join(h for h in haystacks if h)).casefold()
    needle = _WS.sub(" ", cleaned).casefold()
    if needle in hay:
        return cleaned
    tokens = [t for t in _TOKEN.findall(needle) if len(t) >= 3 or t.isdigit()]
    if not tokens:
        return None
    hay_tokens = set(_TOKEN.findall(hay))
    hits = sum(1 for t in tokens if t in hay_tokens)
    return cleaned if hits * 2 >= len(tokens) else None


def guard_offering(value: int | None, active_ids: Iterable[int]) -> int | None:
    if value is None:
        return None
    return value if value in set(active_ids) else None


def guard_language(value: str | None) -> str:
    language = (value or "").strip().lower()[:LANGUAGE_MAX_CHARS]
    return language or "und"


def apply_guards(result: TriageResult, inp: TriageInput, active_offering_ids: Iterable[int]) -> TriageResult:
    """Return a new, guarded ``TriageResult``. ``inp.text`` is the full raw post text."""
    raw = inp.text
    source = (raw, inp.author_name, inp.author_headline)
    company = guard_grounded(result.company, *source)
    contact_person = guard_grounded(result.contact_person, *source)
    city = guard_grounded(result.city, *source)
    topic = guard_grounded(result.topic, *source)
    dates_mentioned = clean_optional(result.dates_mentioned)

    reason = one_line(result.reason, REASON_MAX_CHARS) or NO_REASON

    summary = one_line(result.requirement_summary, SUMMARY_MAX_CHARS)
    if not summary:
        who = company or clean_optional(inp.author_name) or "Unknown"
        what = topic or "AI requirement"
        where = city or result.mode
        summary = one_line(f"{who} - {what} - {where}", SUMMARY_MAX_CHARS)

    priority = "low" if result.verdict == "discard" else result.priority

    return result.model_copy(
        update={
            "reason": reason,
            "requirement_summary": summary,
            "priority": priority,
            "offering_id": guard_offering(result.offering_id, active_offering_ids),
            "company": company,
            "contact_person": contact_person,
            "email": guard_email(result.email, raw),
            "phone": guard_phone(result.phone, raw),
            "city": city,
            "topic": topic,
            "dates_mentioned": dates_mentioned,
            "language": guard_language(result.language),
        }
    )
