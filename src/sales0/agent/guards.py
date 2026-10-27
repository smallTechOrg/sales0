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
    if len(digits) < PHONE_MIN_DIGITS or digits not in _NON_DIGIT.sub("", raw_text):
        return None
    return phone


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
    company = clean_optional(result.company)
    contact_person = clean_optional(result.contact_person)
    city = clean_optional(result.city)
    topic = clean_optional(result.topic)
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
