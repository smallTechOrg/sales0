"""Flags: repeat poster thresholds/aging, competitor mentions, no_follow_up, idempotency, recompute."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from sales0.db.models import CompetitorMention, Flag, FollowUp, Prospect
from sales0.services.crm import flags, followups, prospects, registries
from sales0.services.crm.actor import TEAM
from tests.integration.crm import helpers as h

NOW = h.T0 + timedelta(days=10)


def _author_with(db, n: int, *, ages: list[int] | None = None, accept: bool = True):
    prof = h.profile(db, slug=f"in/rp{h._ids.__next__()}")
    es = []
    for i in range(n):
        age = (ages or [1] * n)[i]
        es.append(h.enquiry(db, profile_row=prof, company="Repeat Co", date_added=NOW - timedelta(days=age)))
    first = prospects.ensure_for_enquiry(db, es[0].id, now=NOW)
    for e in es[1:]:
        if accept:
            prospects.ensure_for_enquiry(db, e.id, now=NOW)
    return first, es, prof


def _active(db, kind, entity="prospect", eid=None):
    return db.scalars(select(Flag).where(Flag.entity_type == entity, Flag.kind == kind, Flag.cleared_at.is_(None),
                                         *([Flag.entity_id == eid] if eid else []))).all()


def test_three_within_sixty_days_flags_repeat_poster_with_detail(db) -> None:
    p, _, _ = _author_with(db, 3)
    view = flags.recompute_repeat_poster(db, p.id, now=NOW)
    assert view is not None and view.kind == "repeat_poster" and view.detail == {"count": 3, "within_days": 60}
    assert [f.kind for f in prospects.get_prospect(db, p.id).flags] == ["repeat_poster"]


def test_two_posts_do_not_flag(db) -> None:
    p, _, _ = _author_with(db, 2)
    assert flags.recompute_repeat_poster(db, p.id, now=NOW) is None and not _active(db, "repeat_poster")


def test_a_third_post_older_than_sixty_days_does_not_count(db) -> None:
    p, _, _ = _author_with(db, 3, ages=[1, 2, 61])
    assert flags.recompute_repeat_poster(db, p.id, now=NOW) is None
    p2, _, _ = _author_with(db, 3, ages=[1, 2, 59])
    assert flags.recompute_repeat_poster(db, p2.id, now=NOW) is not None


def test_unaccepted_posts_of_the_same_profile_count_but_discarded_do_not(db) -> None:
    prof = h.profile(db)
    a = h.enquiry(db, profile_row=prof, company="X", date_added=NOW)
    h.enquiry(db, profile_row=prof, status="pending_review", date_added=NOW)
    h.enquiry(db, profile_row=prof, status="pending_review", date_added=NOW)
    d = h.enquiry(db, profile_row=prof, status="discarded", date_added=NOW)
    v = prospects.ensure_for_enquiry(db, a.id, now=NOW)
    assert flags.recompute_repeat_poster(db, v.id, now=NOW).detail["count"] == 3
    assert d.id  # the discarded one is not part of the count


def test_falling_below_clears_an_auto_flag_but_never_a_user_flag(db) -> None:
    p, es, _ = _author_with(db, 3)
    flags.recompute_repeat_poster(db, p.id, now=NOW)
    es[2].review_status = "discarded"
    db.flush()
    assert flags.recompute_repeat_poster(db, p.id, now=NOW) is None and not _active(db, "repeat_poster")
    flags.set_flag(db, "prospect", p.id, "repeat_poster", source="user")
    assert flags.recompute_repeat_poster(db, p.id, now=NOW) is None
    assert len(_active(db, "repeat_poster")) == 1  # user flag survives the recompute


def test_set_flag_is_idempotent_and_updates_detail(db) -> None:
    p, _, _ = _author_with(db, 1)
    flags.set_flag(db, "prospect", p.id, "repeat_poster", detail={"count": 3})
    flags.set_flag(db, "prospect", p.id, "repeat_poster", detail={"count": 4})
    rows = db.scalars(select(Flag).where(Flag.entity_id == p.id)).all()
    assert len(rows) == 1 and rows[0].detail == {"count": 4}
    assert flags.flags_for(db, "prospect", [p.id, 999])[p.id][0].detail == {"count": 4}
    assert flags.clear_flag(db, "prospect", p.id, "repeat_poster", actor=TEAM) is True
    assert flags.clear_flag(db, "prospect", p.id, "repeat_poster") is False
    assert flags.flags_for(db, "prospect", [p.id]) == {}
    # cleared flags are kept for history and a new active one can be raised again
    flags.set_flag(db, "prospect", p.id, "repeat_poster")
    assert db.scalar(select(func.count(Flag.id)).where(Flag.entity_id == p.id)) == 2


def test_no_follow_up_cancels_reminders_and_clear_does_not_recreate(db) -> None:
    p, _, _ = _author_with(db, 1)
    prospects.mark_contacted(db, p.id, when=h.T0, now=h.T0)
    flags.set_flag(db, "prospect", p.id, "no_follow_up", source="user")
    assert db.scalar(select(func.count(FollowUp.id)).where(FollowUp.prospect_id == p.id, FollowUp.cancelled_at.is_not(None))) == 3
    flags.clear_flag(db, "prospect", p.id, "no_follow_up")
    assert db.scalar(select(func.count(FollowUp.id)).where(FollowUp.prospect_id == p.id, FollowUp.cancelled_at.is_(None), FollowUp.done_at.is_(None))) == 0
    assert followups.due_count(db, today=h.day(3)) == 0


def test_competitor_mention_by_name_and_website_and_boundaries(db) -> None:
    prof = h.profile(db)
    hit = h.enquiry(db, profile_row=prof, company="Acme", raw_text="We were unhappy with Future Labs and want a new partner")
    host = h.enquiry(db, company="Acme", raw_text="see evolvv.example/courses for what we had")
    no = h.enquiry(db, company="Acme", raw_text="the futurelabsxyz startup is unrelated")
    from sales0.db.models import Competitor
    evolvv = db.scalar(select(Competitor).where(Competitor.name_key == "evolvv"))
    registries.update_competitor(db, evolvv.id, website="https://www.evolvv.example")
    prospects.ensure_for_enquiry(db, hit.id)
    flags.detect_competitor_mentions(db, host.id)
    flags.detect_competitor_mentions(db, no.id)
    fl = db.scalar(select(Competitor).where(Competitor.name_key == "future labs"))
    assert flags.detect_competitor_mentions(db, hit.id) == [fl.id]
    assert db.scalar(select(func.count(CompetitorMention.id)).where(CompetitorMention.entity_id == hit.id)) == 1
    assert [f.kind for f in flags.flags_for(db, "enquiry", [hit.id])[hit.id]] == ["competitor_mention"]
    assert flags.flags_for(db, "enquiry", [hit.id])[hit.id][0].detail == {"competitor_ids": [fl.id], "names": ["Future Labs"]}
    pid = hit.prospect_id
    assert [f.kind for f in flags.flags_for(db, "prospect", [pid])[pid]] == ["competitor_mention"]
    assert flags.detect_competitor_mentions(db, host.id) == [evolvv.id]
    assert flags.detect_competitor_mentions(db, no.id) == [] and no.id not in flags.flags_for(db, "enquiry", [no.id])


def test_deactivated_competitor_is_ignored_and_its_flag_cleared(db) -> None:
    from sales0.db.models import Competitor
    fl = db.scalar(select(Competitor).where(Competitor.name_key == "future labs"))
    e = h.enquiry(db, company="Acme", raw_text="Future Labs quoted us last year")
    v = prospects.ensure_for_enquiry(db, e.id)
    assert flags.flags_for(db, "enquiry", [e.id])
    registries.update_competitor(db, fl.id, active=False)
    assert flags.detect_competitor_mentions(db, e.id) == []
    assert flags.flags_for(db, "enquiry", [e.id]) == {} and flags.flags_for(db, "prospect", [v.id]) == {}
    assert db.scalar(select(func.count(CompetitorMention.id))) == 0
    registries.update_competitor(db, fl.id, active=True)
    assert flags.flags_for(db, "enquiry", [e.id])  # re-activation re-scans


def test_recompute_all_counts_and_is_idempotent(db) -> None:
    _author_with(db, 3)
    h.enquiry(db, company="Q", raw_text="Savya Technologies was our last vendor")
    first = flags.recompute_all(db, now=NOW)
    assert first == {"repeat_poster": 1, "competitor_mention": 1}
    n = db.scalar(select(func.count(Flag.id)))
    assert flags.recompute_all(db, now=NOW) == first and db.scalar(select(func.count(Flag.id))) == n


def test_flag_input_validation(db) -> None:
    import pytest

    with pytest.raises(ValueError):
        flags.set_flag(db, "prospect", 1, "bogus")
    with pytest.raises(ValueError):
        flags.set_flag(db, "nothing", 1, "repeat_poster")
    assert flags.flags_for(db, "prospect", []) == {}
    assert db.scalar(select(func.count(Prospect.id))) == 0
