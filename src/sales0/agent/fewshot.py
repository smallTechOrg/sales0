"""Few-shot selection: the learning loop (agent.md -> Few-shot selection).

``load_candidates`` reads the latest team decision per enquiry (read-only).
``select_examples`` is pure and deterministic.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

from sales0.agent.guards import ELLIPSIS
from sales0.agent.schemas import DecisionExample
from sales0.clock import ensure_utc

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def truncate_words(text: str, max_chars: int) -> str:
    """Truncate to at most ``max_chars`` (ellipsis included), cutting on a word boundary."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    budget = max(max_chars - len(ELLIPSIS), 0)
    cut = text[:budget]
    # Cut back to the last whitespace unless the next char already starts a new word.
    if budget < len(text) and not text[budget].isspace():
        space = cut.rfind(" ")
        newline = cut.rfind("\n")
        boundary = max(space, newline)
        if boundary > 0:
            cut = cut[:boundary]
    return cut.rstrip() + ELLIPSIS


def _newest_first(items: Sequence[DecisionExample]) -> list[DecisionExample]:
    return sorted(items, key=lambda e: (ensure_utc(e.decided_at), e.enquiry_id), reverse=True)


def select_examples(candidates: list[DecisionExample], max_n: int, text_chars: int) -> list[DecisionExample]:
    """Pick ≤ ``max_n`` examples: overrides first, then an alternating discard/accept fill.

    1. Overrides (``is_override``), newest first, up to ceil(max_n / 2).
    2. Remaining slots alternate discard/accept, starting with the kind less represented
       among the examples picked so far (discard on a tie), newest first.
    3. If one kind runs out, fill from the other.
    4. Each text is truncated to ``text_chars`` on a word boundary.
    5. Returned oldest first.
    """
    if max_n <= 0 or not candidates:
        return []

    ordered = _newest_first(candidates)
    picked: list[DecisionExample] = []
    override_cap = math.ceil(max_n / 2)
    for cand in ordered:
        if len(picked) >= override_cap:
            break
        if cand.is_override:
            picked.append(cand)

    picked_ids = {id(e) for e in picked}
    pools: dict[str, list[DecisionExample]] = {
        "discard": [c for c in ordered if id(c) not in picked_ids and c.decision == "discard"],
        "accept": [c for c in ordered if id(c) not in picked_ids and c.decision == "accept"],
    }
    n_discard = sum(1 for e in picked if e.decision == "discard")
    n_accept = len(picked) - n_discard
    kind = "accept" if n_accept < n_discard else "discard"
    other = {"discard": "accept", "accept": "discard"}

    while len(picked) < max_n and (pools["discard"] or pools["accept"]):
        if not pools[kind]:
            kind = other[kind]
        picked.append(pools[kind].pop(0))
        kind = other[kind]

    chronological = sorted(picked, key=lambda e: (ensure_utc(e.decided_at), e.enquiry_id))
    return [e.model_copy(update={"text": truncate_words(e.text, text_chars)}) for e in chronological]


def load_candidates(db: Session, exclude_enquiry_id: int, limit: int = 200) -> list[DecisionExample]:
    """The latest decision per enquiry (by ``decided_at``, then id), newest first, with the post text.

    Read-only. Uses a window function (SQLite ≥3.25 and PostgreSQL both support it).
    """
    from sqlalchemy import func, select

    from sales0.db.models import Enquiry, TriageDecision

    ranked = select(
        TriageDecision.id.label("decision_id"),
        TriageDecision.enquiry_id.label("enquiry_id"),
        func.row_number()
        .over(
            partition_by=TriageDecision.enquiry_id,
            order_by=(TriageDecision.decided_at.desc(), TriageDecision.id.desc()),
        )
        .label("rn"),
    ).subquery()

    stmt = (
        select(TriageDecision, Enquiry.raw_text)
        .join(ranked, ranked.c.decision_id == TriageDecision.id)
        .join(Enquiry, Enquiry.id == TriageDecision.enquiry_id)
        .where(ranked.c.rn == 1, TriageDecision.enquiry_id != exclude_enquiry_id)
        .order_by(TriageDecision.decided_at.desc(), TriageDecision.id.desc())
        .limit(limit)
    )
    examples: list[DecisionExample] = []
    for decision, raw_text in db.execute(stmt).all():
        examples.append(
            DecisionExample(
                enquiry_id=decision.enquiry_id,
                text=raw_text or "",
                decision=decision.decision,
                ai_verdict=decision.ai_verdict,
                is_override=bool(decision.is_override),
                reason=decision.reason,
                decided_at=ensure_utc(decision.decided_at),
            )
        )
    return examples
