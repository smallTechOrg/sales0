"""Audit @7ae3d66 data findings #2 (concurrent accepts), #3 (idempotent decide), #14 (timeline per decision),
#15 (healer for accepted enquiries without a prospect)."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, Prospect, StatusHistory, TimelineEvent, TriageDecision
from sales0.services import crm, events, inbox
from sales0.services.crm import prospects
from tests.integration.crm import helpers as h


@pytest.fixture
def hooked(db_url: str):  # type: ignore[no-untyped-def]
    events.reset()
    crm.install()
    yield
    events.reset()


def _pending_profileless() -> int:
    with session_scope() as db:
        e = h.enquiry(db, status="pending_review", company="Beta Corp", ai_verdict="genuine")
        e.author_profile_url = None
        e.author_profile_id = None
        return e.id


def _count(model, *where):  # type: ignore[no-untyped-def]
    with session_scope() as db:
        return db.scalar(select(func.count()).select_from(model).where(*where))


# ------------------------------------------------------------------------------------------ #2
def test_two_simultaneous_accepts_of_a_profileless_enquiry_make_one_prospect(hooked) -> None:  # type: ignore[no-untyped-def]
    eid = _pending_profileless()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def accept() -> None:
        try:
            barrier.wait(timeout=10)
            with session_scope() as db:
                inbox.decide(db, [eid], "accept")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=accept) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert _count(Prospect, Prospect.origin_enquiry_id == eid) == 1
    assert _count(TriageDecision, TriageDecision.enquiry_id == eid) == 1  # the loser is an idempotent no-op (#3)


def test_ensure_for_enquiry_reuses_the_prospect_that_already_names_it_as_origin(db, hooked) -> None:  # type: ignore[no-untyped-def]
    e = h.enquiry(db, company="Beta Corp")
    first = prospects.ensure_for_enquiry(db, e.id)
    e.prospect_id = None  # the link was lost (failed hook / manual edit): origin still points at the enquiry
    db.flush()
    again = prospects.ensure_for_enquiry(db, e.id)
    assert again.id == first.id
    assert db.scalar(select(func.count()).select_from(Prospect).where(Prospect.origin_enquiry_id == e.id)) == 1


def test_unique_origin_index_rejects_a_second_prospect(db) -> None:  # type: ignore[no-untyped-def]
    e = h.enquiry(db)
    db.add(Prospect(name="A", origin_enquiry_id=e.id))
    db.flush()
    db.add(Prospect(name="B", origin_enquiry_id=e.id))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_ensure_for_enquiry_recovers_from_a_lost_race(db, hooked, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The pre-insert lookup misses (another writer committed in between): the IntegrityError is turned into a re-read."""
    e = h.enquiry(db, company="Beta Corp")
    winner = Prospect(name="Beta Corp", origin_enquiry_id=e.id)
    db.add(winner)
    db.flush()
    real, calls = prospects._by_origin, []

    def miss_once(_db, _eid):  # type: ignore[no-untyped-def]
        calls.append(1)
        return None if len(calls) == 1 else real(_db, _eid)

    monkeypatch.setattr(prospects, "_by_origin", miss_once, raising=True)
    view = prospects.ensure_for_enquiry(db, e.id)
    assert view.id == winner.id
    assert db.scalar(select(func.count()).select_from(Prospect).where(Prospect.origin_enquiry_id == e.id)) == 1


# ------------------------------------------------------------------------------------------ #3
def test_second_accept_is_a_noop_no_decision_no_history(db, hooked) -> None:  # type: ignore[no-untyped-def]
    e = h.enquiry(db, status="pending_review", company="Beta Corp", ai_verdict="genuine")
    inbox.decide(db, [e.id], "accept")
    db.commit()
    n_dec = db.scalar(select(func.count()).select_from(TriageDecision).where(TriageDecision.enquiry_id == e.id))
    n_hist = db.scalar(select(func.count()).select_from(StatusHistory).where(StatusHistory.entity_id == e.id))
    [card] = inbox.decide(db, [e.id], "accept")
    assert card.review_status == "accepted"
    assert db.scalar(select(func.count()).select_from(TriageDecision).where(TriageDecision.enquiry_id == e.id)) == n_dec
    assert db.scalar(select(func.count()).select_from(StatusHistory).where(StatusHistory.entity_id == e.id)) == n_hist


def test_second_discard_is_a_noop_but_flip_still_records(db, hooked) -> None:  # type: ignore[no-untyped-def]
    e = h.enquiry(db, status="pending_review", ai_verdict="genuine", ai_reason="r")
    inbox.decide(db, [e.id], "discard", "Not relevant")
    inbox.decide(db, [e.id], "discard", "Not relevant")
    assert db.scalar(select(func.count()).select_from(TriageDecision).where(TriageDecision.enquiry_id == e.id)) == 1
    inbox.decide(db, [e.id], "accept")
    assert db.scalar(select(func.count()).select_from(TriageDecision).where(TriageDecision.enquiry_id == e.id)) == 2


# ------------------------------------------------------------------------------------------ #14
def _enquiry_events(db, eid):  # type: ignore[no-untyped-def]
    return list(db.scalars(select(TimelineEvent).where(TimelineEvent.enquiry_id == eid).order_by(TimelineEvent.id)))


def test_every_decision_has_a_timeline_event_with_actor(db, hooked) -> None:  # type: ignore[no-untyped-def]
    m = h.member(db, "Priya")
    e = h.enquiry(db, status="pending_review", ai_verdict="genuine", ai_reason="fits")
    inbox.decide(db, [e.id], "discard", "Not relevant", actor_member_id=m.id)  # discard without a prospect
    ev = [t for t in _enquiry_events(db, e.id) if (t.data or {}).get("decision") == "discard"]
    assert len(ev) == 1 and ev[0].actor_member_id == m.id and ev[0].actor.startswith("Priya")
    assert ev[0].subject_type == "enquiry"
    inbox.decide(db, [e.id], "accept", actor_member_id=m.id)
    inbox.decide(db, [e.id], "accept", actor_member_id=m.id)  # no-op repeat: still audited
    decisions = [t for t in _enquiry_events(db, e.id) if (t.data or {}).get("decision") in ("accept", "accept_repeat")]
    assert [(t.data or {}).get("decision") for t in decisions] == ["accept", "accept_repeat"]
    assert all(t.actor_member_id == m.id for t in decisions)


def test_priority_and_offering_edits_leave_a_timeline_event(db, hooked) -> None:  # type: ignore[no-untyped-def]
    m = h.member(db, "Ravi")
    e = h.enquiry(db, status="pending_review", ai_verdict="genuine", priority="medium")
    inbox.update_fields(db, e.id, priority="high", actor_user_id=m.id)
    ev = [t for t in _enquiry_events(db, e.id) if (t.data or {}).get("edit")]
    assert len(ev) == 1 and ev[0].actor_member_id == m.id
    assert (ev[0].data or {})["fields"] == {"priority": ["medium", "high"]}
    inbox.update_fields(db, e.id, priority="high", actor_user_id=m.id)  # unchanged: nothing new
    assert len([t for t in _enquiry_events(db, e.id) if (t.data or {}).get("edit")]) == 1


# ------------------------------------------------------------------------------------------ #15
def test_failed_hook_leaves_an_audit_event_and_the_healer_creates_the_prospect(db, hooked, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    e = h.enquiry(db, status="pending_review", company="Beta Corp", ai_verdict="genuine")
    real = prospects.ensure_for_enquiry

    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr(prospects, "ensure_for_enquiry", boom)
    inbox.decide(db, [e.id], "accept")
    db.commit()
    assert db.get(Enquiry, e.id).prospect_id is None
    audit = [t for t in _enquiry_events(db, e.id) if (t.data or {}).get("hook_failed")]
    assert len(audit) == 1 and (audit[0].data or {})["hook"] == "_on_enquiry_decided"
    monkeypatch.setattr(prospects, "ensure_for_enquiry", real)
    healed = crm.heal_accepted_without_prospect(db)
    assert healed == 1
    assert db.get(Enquiry, e.id).prospect_id is not None
    assert crm.heal_accepted_without_prospect(db) == 0  # idempotent


def test_startup_heals_and_the_nightly_purge_job_runs_the_healer(db, hooked) -> None:  # type: ignore[no-untyped-def]
    from sales0.jobs.scheduler import purge_hooks

    e = h.enquiry(db, status="accepted", company="Gamma Ltd")  # accepted, prospect_id NULL (e.g. a crashed hook)
    db.commit()
    assert "crm_heal_orphan_accepts" in purge_hooks()
    crm.startup_backfill(db)  # app startup
    db.commit()
    assert db.get(Enquiry, e.id).prospect_id is not None
    e2 = h.enquiry(db, status="accepted", company="Delta Ltd")
    db.commit()
    from datetime import datetime

    assert purge_hooks()["crm_heal_orphan_accepts"](db, datetime.now()) == 1  # what the scheduler's purge job calls
    assert db.get(Enquiry, e2.id).prospect_id is not None
