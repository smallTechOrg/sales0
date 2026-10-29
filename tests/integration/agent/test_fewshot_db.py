"""The learning loop against a real migrated SQLite file: load_candidates reads the latest
decision per enquiry, and a real triage call uses the selected examples."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.agent.fewshot import load_candidates, select_examples
from sales0.agent.schemas import OfferingRef
from sales0.config import get_settings
from sales0.db.engine import get_sessionmaker
from sales0.db.models import Enquiry, Offering, TriageDecision
from tests.integration.agent.conftest import assert_well_formed, triage_real
from tests.integration.agent.corpus import CORPUS

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def _enquiry(db: Session, text: str, ai_verdict: str | None) -> Enquiry:
    row = Enquiry(
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        raw_text=text,
        ai_verdict=ai_verdict,
        triage_status="done",
    )
    db.add(row)
    db.flush()
    return row


def _decide(db: Session, enq: Enquiry, decision: str, minutes: int, reason: str | None = None) -> TriageDecision:
    is_override = (decision == "accept" and enq.ai_verdict == "discard") or (
        decision == "discard" and enq.ai_verdict == "genuine"
    )
    row = TriageDecision(
        enquiry_id=enq.id,
        decision=decision,
        ai_verdict=enq.ai_verdict,
        is_override=is_override,
        reason=reason,
        decided_at=T0 + timedelta(minutes=minutes),
    )
    db.add(row)
    db.flush()
    return row


def _active_offerings(db: Session) -> list[OfferingRef]:
    rows = db.scalars(select(Offering).where(Offering.active.is_(True)).order_by(Offering.sort_order, Offering.id))
    return [OfferingRef(id=o.id, name=o.name, group=o.group.name, description=o.description) for o in rows]


def test_fresh_db_has_no_candidates_so_cold_start_uses_zero_examples(db_url: str) -> None:
    with get_sessionmaker()() as db:
        assert load_candidates(db, exclude_enquiry_id=0) == []
        assert select_examples([], 6, 600) == []


def test_load_candidates_returns_the_latest_decision_per_enquiry_newest_first(db_url: str) -> None:
    with get_sessionmaker()() as db:
        a = _enquiry(db, "Bank wants a GenAI workshop for 30 managers", "genuine")
        b = _enquiry(db, "Agency onboarding freelance trainers at per-day rates", "discard")
        c = _enquiry(db, "Startup wants hands-on agents training", "discard")
        current = _enquiry(db, "The enquiry being triaged now", "genuine")
        _decide(db, a, "accept", 1)
        _decide(db, a, "discard", 5, reason="Too vague")  # re-decided: latest wins
        _decide(db, b, "discard", 2, reason="Agency recruiting trainers")
        _decide(db, c, "accept", 3)  # override of the AI's discard
        _decide(db, current, "accept", 9)  # excluded
        db.commit()

        cands = load_candidates(db, exclude_enquiry_id=current.id)

    assert [c_.enquiry_id for c_ in cands] == [a.id, c.id, b.id]
    latest_a = cands[0]
    assert latest_a.decision == "discard" and latest_a.reason == "Too vague" and latest_a.is_override
    assert latest_a.text == "Bank wants a GenAI workshop for 30 managers"
    assert latest_a.decided_at == T0 + timedelta(minutes=5)
    assert cands[1].is_override and cands[1].ai_verdict == "discard" and cands[1].decision == "accept"
    assert not cands[2].is_override

    with get_sessionmaker()() as db:
        limited = load_candidates(db, exclude_enquiry_id=current.id, limit=1)
    assert [c_.enquiry_id for c_ in limited] == [a.id]


@pytest.mark.llm
def test_triage_after_decisions_uses_fewshot_examples_and_db_offerings(db_url: str) -> None:
    settings = get_settings()
    with get_sessionmaker()() as db:
        offerings = _active_offerings(db)
        assert [o.name for o in offerings] == [
            "AI corporate workshops",
            "Technical AI training",
            "AI consulting & build services",
            "Products",
        ]
        accepted = _enquiry(db, CORPUS["P1"].text, "genuine")
        discarded = _enquiry(db, CORPUS["P2"].text, "discard")
        overridden = _enquiry(db, CORPUS["P6"].text, "discard")
        _decide(db, accepted, "accept", 1)
        _decide(db, discarded, "discard", 2, reason="Agency recruiting freelance trainers")
        _decide(db, overridden, "accept", 3)
        db.commit()
        target = _enquiry(db, CORPUS["P8"].text, None)
        db.commit()
        candidates = load_candidates(db, exclude_enquiry_id=target.id)

    examples = select_examples(candidates, settings.fewshot_max_examples, settings.fewshot_text_chars)
    assert len(examples) == 3 and examples[-1].is_override

    inp = CORPUS["P8"].model_copy(update={"enquiry_id": target.id})
    outcome = triage_real(inp, offerings=offerings, examples=examples)

    assert_well_formed(outcome)
    assert outcome.fewshot_count == len(examples) == 3
    assert outcome.result is not None
    assert outcome.result.verdict == "genuine"
    consulting_id = next(o.id for o in offerings if o.name == "AI consulting & build services")
    assert outcome.result.offering_id == consulting_id
