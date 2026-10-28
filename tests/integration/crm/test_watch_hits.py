"""CRM x prospect watch: TrackedProfileHit timeline/follow-up and the tracked toggle through the CRM."""

from __future__ import annotations

from sqlalchemy import select

from sales0.db.models import FollowUp, Lead, Prospect, TimelineEvent
from sales0.services import events, tracked_profiles
from sales0.services.crm import flags, prospects
from tests.integration.crm import helpers as h


def _prospect(db):
    prof = h.profile(db, slug=f"in/w{h._ids.__next__()}")
    prof.tracked = True
    e = h.enquiry(db, profile_row=prof, company="Acme Training")
    v = prospects.ensure_for_enquiry(db, e.id)
    return v, prof


def _hit(db, prof, status="pending_review"):
    e = h.enquiry(db, status=status, profile_row=prof, company="Acme Training")
    events.publish(db, events.TrackedProfileHit(profile_id=prof.id, enquiry_id=e.id, run_id=7))
    return e


def _notes(db, pid):
    return [t for t in db.scalars(select(TimelineEvent).where(TimelineEvent.prospect_id == pid, TimelineEvent.kind == "note"))
            if (t.data or {}).get("watch_enquiry_id")]


def test_hit_adds_timeline_note_and_is_idempotent(db, installed) -> None:
    v, prof = _prospect(db)
    e = _hit(db, prof)
    events.publish(db, events.TrackedProfileHit(profile_id=prof.id, enquiry_id=e.id, run_id=7))
    notes = _notes(db, v.id)
    assert len(notes) == 1 and notes[0].body == "New requirement from tracked profile" and notes[0].enquiry_id == e.id
    assert db.scalar(select(FollowUp).where(FollowUp.prospect_id == v.id, FollowUp.kind == "custom")) is None
    db.refresh(e)
    assert e.prospect_id is None  # pending: linked by the accept flow


def test_hit_on_accepted_enquiry_attaches_it(db, installed) -> None:
    v, prof = _prospect(db)
    e = _hit(db, prof, status="accepted")
    db.refresh(e)
    assert e.prospect_id == v.id


def test_hit_on_final_follow_up_needs_follow_up(db, installed) -> None:
    v, prof = _prospect(db)
    p = db.get(Prospect, v.id)
    p.stage = "final_follow_up"
    db.flush()
    _hit(db, prof)
    fu = db.scalars(select(FollowUp).where(FollowUp.prospect_id == v.id, FollowUp.kind == "custom")).all()
    assert len(fu) == 1 and fu[0].cancelled_at is None
    assert "follow-up needed" in _notes(db, v.id)[0].body


def test_hit_on_closed_no_response_needs_follow_up_and_stays_closed(db, installed) -> None:
    v, prof = _prospect(db)
    prospects.close(db, v.id, "no_response")
    assert db.get(h.LinkedinProfile, prof.id).tracked is False
    _hit(db, prof)
    assert db.scalar(select(FollowUp).where(FollowUp.prospect_id == v.id, FollowUp.kind == "custom")) is not None
    assert db.get(Prospect, v.id).status == "closed"


def test_hit_respects_no_follow_up_flag_and_ignores_unknown_profile(db, installed) -> None:
    v, prof = _prospect(db)
    prospects.close(db, v.id, "lost")
    flags.set_flag(db, "prospect", v.id, "no_follow_up", source="user")
    _hit(db, prof)
    assert db.scalar(select(FollowUp).where(FollowUp.prospect_id == v.id, FollowUp.kind == "custom")) is None
    other = h.profile(db, slug="in/nobody")
    _hit(db, other)  # no prospect: no error
    assert db.scalar(select(Lead.id)) is None


def test_watch_page_toggle_goes_through_crm_timeline(db, installed) -> None:
    v, prof = _prospect(db)
    tracked_profiles.untrack(db, prof.id)
    tracked_profiles.track(db, prof.id)
    kinds = [t.body for t in db.scalars(select(TimelineEvent).where(TimelineEvent.prospect_id == v.id, TimelineEvent.kind == "tracked_changed").order_by(TimelineEvent.id))]
    assert kinds == ["No longer tracked", "Now tracked"]
    assert db.get(h.LinkedinProfile, prof.id).tracked is True


def test_toggle_without_prospect_just_sets_flag(db, installed) -> None:
    prof = h.profile(db, slug="in/lonely")
    tracked_profiles.track(db, prof.id)
    assert db.get(h.LinkedinProfile, prof.id).tracked is True
