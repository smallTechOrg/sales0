"""Inbox feedback A3: services.criteria on a migrated SQLite DB (seed, edits, versions, tracing, breakdown)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.agent.criteria import DEFAULT_SET
from sales0.clock import utcnow
from sales0.db.models import Enquiry, LlmCall, SeenPost, TriageCriteriaSet, TriageCriterion
from sales0.errors import DomainError
from sales0.services import criteria, inbox
from sales0.services.triage import triage_enquiry
from tests.support.fake_llm import FakeLLMClient, triage_answer


def test_migration_seeds_the_built_in_rules_as_version_1(db: Session) -> None:
    active = criteria.active_set(db)
    assert active.version == 1 and active.rules == DEFAULT_SET.rules
    rules = criteria.list_rules(db)
    assert [r.kind for r in rules] == ["include"] * 5 + ["exclude"] * 8
    assert {r.source for r in rules} == {"seed"} and all(r.active for r in rules)
    snap = db.get(TriageCriteriaSet, 1)
    assert snap is not None and len(snap.rules) == 13


def test_every_change_writes_a_new_snapshot_and_the_prompt_follows(db: Session) -> None:
    added = criteria.create_rule(db, "exclude", "  A college asking for   free guest lectures.  ")
    assert added.text == "A college asking for free guest lectures." and added.source == "team"
    assert criteria.current_version(db) == 2
    assert criteria.active_set(db).exclude[-1] == added.text

    first_include = criteria.list_rules(db)[0]
    criteria.set_active(db, first_include.id, False)
    assert criteria.current_version(db) == 3 and first_include.text not in criteria.active_set(db).include
    criteria.set_active(db, first_include.id, False)  # no change -> no new version
    assert criteria.current_version(db) == 3

    criteria.move(db, added.id, "up")
    assert criteria.active_set(db).exclude[-2] == added.text and criteria.current_version(db) == 4
    criteria.update_rule(db, added.id, text="A college asking for free guest lectures or student workshops.")
    criteria.update_rule(db, added.id, kind="include")
    assert criteria.active_set(db).include[-1].startswith("A college") and criteria.current_version(db) == 6
    snap = db.get(TriageCriteriaSet, 6)
    assert snap is not None and {"kind": "include", "text": "A college asking for free guest lectures or student workshops."} in snap.rules


def test_validation_and_duplicates(db: Session) -> None:
    with pytest.raises(DomainError) as e:
        criteria.create_rule(db, "exclude", "no")
    assert e.value.code == "criterion_invalid"
    with pytest.raises(DomainError):
        criteria.create_rule(db, "maybe", "A perfectly fine rule text")
    with pytest.raises(DomainError):
        criteria.create_rule(db, "exclude", "x" * 601)
    criteria.create_rule(db, "exclude", "Hackathon sponsorship requests")
    with pytest.raises(DomainError) as dup:
        criteria.create_rule(db, "exclude", "hackathon sponsorship requests")
    assert dup.value.code == "criterion_exists"
    assert criteria.rule_from_discard(db, "Hackathon sponsorship requests", enquiry_id=None) is None


def test_rule_from_discard_reenables_a_disabled_rule(db: Session) -> None:
    rule = criteria.create_rule(db, "exclude", "Hackathon sponsorship requests")
    criteria.set_active(db, rule.id, False)
    again = criteria.rule_from_discard(db, "Hackathon sponsorship requests", enquiry_id=5)
    assert again is not None and again.id == rule.id and again.active
    assert db.scalar(select(TriageCriterion).where(TriageCriterion.id == rule.id)).active  # type: ignore[union-attr]


def test_empty_table_falls_back_to_the_built_in_defaults(db: Session) -> None:
    for row in db.scalars(select(TriageCriterion)).all():
        db.delete(row)
    db.flush()
    assert criteria.active_set(db) is DEFAULT_SET


def test_a_triage_call_records_the_rule_set_and_sends_the_team_rules(db: Session) -> None:
    rule = criteria.create_rule(db, "exclude", "Posts asking for free webinars from vendors.")
    e = Enquiry(content_hash="h-crit", raw_text="Need an AI workshop for 30 managers", date_added=utcnow())
    db.add(e)
    db.flush()
    llm = FakeLLMClient([triage_answer()])
    triage_enquiry(db, e.id, llm)
    call = db.scalar(select(LlmCall).where(LlmCall.enquiry_id == e.id))
    assert call is not None and call.criteria_version == criteria.current_version(db) == 2
    assert rule.text in llm.requests[0].system


def _discarded(db: Session, n: int, reason: str | None, *, days_ago: int = 0, auto: bool = False) -> Enquiry:
    e = Enquiry(content_hash=f"h{n}", raw_text=f"Synthetic post {n} about AI training", date_added=utcnow(),
                triage_status="done", ai_verdict="genuine", ai_reason="Buyer", review_status="pending_review",
                source_url=f"https://www.linkedin.com/in/poster-{n}-fx/")
    db.add(e)
    db.flush()
    if auto:
        e.review_status, e.discard_source, e.discard_reason = "discarded", "auto", "Repeat from existing prospect X (already 1 this run)"
        e.decided_at = utcnow() - timedelta(days=days_ago)
    else:
        inbox.decide(db, [e.id], "discard", discard_reason=reason, now=utcnow() - timedelta(days=days_ago))
    return e


def test_discard_breakdown_groups_team_system_and_ai(db: Session) -> None:
    _discarded(db, 1, "Agency reselling our workshop")
    _discarded(db, 2, "agency reselling our workshop", days_ago=60)
    _discarded(db, 3, "Not a requirement")
    _discarded(db, 4, None, auto=True)
    db.add(SeenPost(fingerprint="f1", content_hash="c1", keyword_text="k", triage_status="done", verdict="discard",
                    reason="Job post", poster_type="job_seeker_or_self_promo", snippet="Hiring a full-time trainer",
                    first_seen_at=utcnow() - timedelta(days=90), last_seen_at=utcnow()))
    db.flush()
    b = criteria.discard_breakdown(db)
    team = {g.label.casefold(): (g.count_30d, g.count_all, len(g.examples)) for g in b.team}
    assert team["agency reselling our workshop"] == (1, 2, 2)
    assert team["not a requirement"] == (1, 1, 1)
    assert [(g.count_30d, g.count_all) for g in b.system] == [(1, 1)]
    assert [(g.label, g.count_30d, g.count_all) for g in b.ai] == [("AI: Job seeker / self-promo", 0, 1)]
    assert b.ai[0].examples[0].snippet.startswith("Hiring a full-time trainer")
    assert b.totals["team"] == (2, 3)
