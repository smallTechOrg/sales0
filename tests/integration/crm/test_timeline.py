"""The timeline: scopes, merge of Phase 1 status history, ordering, notes, validation."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from sales0.db.models import StatusHistory, TimelineEvent
from sales0.errors import DomainError
from sales0.services.crm import timeline
from sales0.services.crm import prospects
from sales0.services.crm.actor import SHEET_SYNC, Actor
from tests.integration.crm import helpers as h


def test_add_event_derives_scopes_from_the_subject(db) -> None:
    e = h.enquiry(db, company="Acme Training", profile_row=h.profile(db))
    v = prospects.ensure_for_enquiry(db, e.id)
    ev = timeline.add_event(db, subject_type="enquiry", subject_id=e.id, kind="research", body="brief ready")
    assert (ev.enquiry_id, ev.prospect_id, ev.company_id) == (e.id, v.id, v.company_id)
    m = h.member(db, "Priya")
    call = timeline.add_event(db, subject_type="prospect", subject_id=v.id, kind="call", body="spoke", actor=Actor.of_member(m),
                              occurred_at=h.T0)
    assert call.prospect_id == v.id and call.actor == "Priya" and call.actor_member_id == m.id and call.data is None
    for bad in ({"subject_type": "banana", "kind": "note"}, {"subject_type": "prospect", "kind": "banana"}):
        with pytest.raises(ValueError):
            timeline.add_event(db, subject_id=1, **bad)


def test_company_timeline_unions_prospects_enquiries_and_phase1_history(db) -> None:
    prof1, prof2 = h.profile(db, "in/a1"), h.profile(db, "in/a2")
    e1 = h.enquiry(db, profile_row=prof1, company="Acme Training")
    e2 = h.enquiry(db, profile_row=prof2, company="Acme Training")
    v1, v2 = prospects.ensure_for_enquiry(db, e1.id, now=h.T0), prospects.ensure_for_enquiry(db, e2.id, now=h.T0 + timedelta(hours=1))
    db.add(StatusHistory(entity_type="enquiry", entity_id=e1.id, field="review_status", from_value="pending_review",
                         to_value="accepted", changed_at=h.T0 - timedelta(hours=1)))
    db.flush()
    assert v1.company_id == v2.company_id
    items = timeline.list_timeline(db, company_id=v1.company_id)
    kinds = [i.kind for i in items]
    assert kinds.count("created") == 3 and kinds.count("enquiry_linked") == 2 and "stage_change" in kinds
    assert [i.occurred_at for i in items] == sorted((i.occurred_at for i in items), reverse=True)  # newest first
    hist = next(i for i in items if i.data and i.data.get("source") == "status_history")
    assert hist.id < 0 and hist.actor == "team" and "pending_review" in (hist.body or "")
    # the prospect's own page merges the history of its enquiries too
    assert any(i.id < 0 for i in timeline.list_timeline(db, prospect_id=v1.id))
    assert not any(i.id < 0 for i in timeline.list_timeline(db, prospect_id=v2.id))


def test_list_requires_exactly_one_scope(db) -> None:
    with pytest.raises(ValueError):
        timeline.list_timeline(db)
    with pytest.raises(ValueError):
        timeline.list_timeline(db, prospect_id=1, company_id=1)
    assert timeline.list_timeline(db, subject=("competitor", 1)) == []


def test_limit_before_and_ordering(db) -> None:
    e = h.enquiry(db, company="X")
    v = prospects.ensure_for_enquiry(db, e.id, now=h.T0)
    for i in range(5):
        timeline.add_event(db, subject_type="prospect", subject_id=v.id, kind="call", body=f"c{i}", occurred_at=h.T0 + timedelta(minutes=i + 1))
    top = timeline.list_timeline(db, prospect_id=v.id, limit=3)
    assert [t.body for t in top] == ["c4", "c3", "c2"]
    older = timeline.list_timeline(db, prospect_id=v.id, before=h.T0 + timedelta(minutes=3))
    assert [t.body for t in older][:2] == ["c1", "c0"]


def test_notes_are_validated_scoped_and_attributed(db) -> None:
    e = h.enquiry(db, company="X")
    v = prospects.ensure_for_enquiry(db, e.id)
    m = h.member(db, "Asha")
    item = timeline.add_note(db, subject_type="prospect", subject_id=v.id, body="  Spoke to HR  ", actor=Actor.of_member(m), now=h.T0)
    assert item.kind == "note" and item.body == "Spoke to HR" and item.actor == "Asha" and item.href == f"/pipeline/prospects/{v.id}"
    row = db.get(TimelineEvent, item.id)
    assert row.prospect_id == v.id and row.actor_member_id == m.id
    for bad in ("", "   ", "x" * 4001):
        with pytest.raises(DomainError) as ei:
            timeline.add_note(db, subject_type="prospect", subject_id=v.id, body=bad, actor=Actor("x"))
        assert ei.value.code == "note_invalid" and ei.value.http_status == 400
    with pytest.raises(DomainError) as nf:
        timeline.add_note(db, subject_type="prospect", subject_id=424242, body="orphan", actor=Actor("x"))
    assert nf.value.http_status == 404
    assert timeline.add_note(db, subject_type="prospect", subject_id=v.id, body="x" * 4000, actor=Actor("x")).body == "x" * 4000


def test_sheet_sync_events_keep_their_actor(db) -> None:
    e = h.enquiry(db, company="X")
    v = prospects.ensure_for_enquiry(db, e.id)
    timeline.add_event(db, subject_type="prospect", subject_id=v.id, kind="sheet_updated", body="Updated from sheet: priority, notes",
                       actor=SHEET_SYNC)
    ev = db.scalar(select(TimelineEvent).where(TimelineEvent.kind == "sheet_updated"))
    assert ev.actor == "sheet-sync" and ev.actor_member_id is None
    assert timeline.list_timeline(db, prospect_id=v.id)[0].kind_label == "Updated from sheet"
