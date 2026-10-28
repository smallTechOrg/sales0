"""Prospects: accept idempotency, one per profile, stage engine, close/reopen, manual, lists."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from sales0.db.models import ContactLink, Enquiry, FollowUp, LinkedinProfile, Prospect, TimelineEvent
from sales0.errors import DomainError
from sales0.services.crm import flags, prospects
from sales0.services.crm.actor import SYSTEM, Actor
from tests.integration.crm import helpers as h


def kinds(db, pid: int) -> list[str]:
    return [t.kind for t in prospects.get_prospect(db, pid).timeline]


def test_two_enquiries_from_one_author_make_one_prospect(db) -> None:
    prof = h.profile(db)
    e1 = h.enquiry(db, profile_row=prof, company="Acme Training")
    e2 = h.enquiry(db, profile_row=prof, company="Acme Training")
    v1 = prospects.ensure_for_enquiry(db, e1.id)
    v2 = prospects.ensure_for_enquiry(db, e2.id)
    assert v1.id == v2.id
    assert db.scalar(select(func.count(Prospect.id))) == 1
    db.refresh(e1), db.refresh(e2), db.refresh(prof)
    assert e1.prospect_id == e2.prospect_id == v1.id and prof.tracked is True
    ks = kinds(db, v1.id)
    assert ks.count("created") == 1 and ks.count("enquiry_linked") == 2
    d = prospects.get_prospect(db, v1.id)
    assert {x.id for x in d.enquiries} == {e1.id, e2.id}
    assert d.company_name == "Acme Training" and d.linkedin_url == prof.profile_url


def test_accepting_the_same_enquiry_twice_changes_nothing(db) -> None:
    e = h.enquiry(db, profile_row=h.profile(db), company="Acme Training")
    v = prospects.ensure_for_enquiry(db, e.id)
    before = db.scalar(select(func.count(TimelineEvent.id)))
    again = prospects.ensure_for_enquiry(db, e.id)
    assert again.id == v.id and db.scalar(select(func.count(TimelineEvent.id))) == before


def test_without_a_profile_each_enquiry_gets_its_own_prospect(db) -> None:
    a, b = h.enquiry(db, company="Beta Ltd"), h.enquiry(db, company="Beta Ltd")
    assert prospects.ensure_for_enquiry(db, a.id).id != prospects.ensure_for_enquiry(db, b.id).id


def test_profile_is_created_from_the_author_url_when_missing(db) -> None:
    e = h.enquiry(db, author_profile_url="https://www.linkedin.com/in/new-person/", author_name="New Person")
    v = prospects.ensure_for_enquiry(db, e.id)
    prof = db.scalar(select(LinkedinProfile).where(LinkedinProfile.slug == "in/new-person"))
    assert prof is not None and prof.tracked is True and prof.posts_seen == 0
    assert v.linkedin_profile_id == prof.id and v.name == "New Person"


def test_accept_links_company_and_contacts_and_dedupes_a_shared_email(db) -> None:
    prof = h.profile(db)
    e1 = h.enquiry(db, profile_row=prof, company="Acme Training", email="asha@acme.example", contact_person="Asha Rao")
    e2 = h.enquiry(db, company="Acme Training", email="ASHA@acme.example", contact_person="Asha R")
    v1 = prospects.ensure_for_enquiry(db, e1.id)
    prospects.ensure_for_enquiry(db, e2.id)
    db.refresh(e1), db.refresh(e2)
    assert e1.company_id and e1.company_id == e2.company_id
    assert e1.contact_id == e2.contact_id  # same email -> one contact
    links = db.scalars(select(ContactLink).where(ContactLink.entity_type == "prospect", ContactLink.entity_id == v1.id)).all()
    assert {ln.role for ln in links} >= {"buyer", "author"}
    assert prospects.get_prospect(db, v1.id).contacts


def test_discarded_prospect_is_reopened_on_reaccept(db) -> None:
    e = h.enquiry(db, profile_row=h.profile(db), company="Acme Training")
    v = prospects.ensure_for_enquiry(db, e.id)
    prospects.close(db, v.id, "enquiry_discarded")
    assert prospects.get_prospect(db, v.id).status == "closed"
    prospects.ensure_for_enquiry(db, e.id)
    assert prospects.get_prospect(db, v.id).status == "open"


def test_unknown_enquiry_is_not_found(db) -> None:
    with pytest.raises(DomainError) as ei:
        prospects.ensure_for_enquiry(db, 9999)
    assert ei.value.code == "not_found" and ei.value.http_status == 404


def _prospect(db, **kw):
    e = h.enquiry(db, profile_row=h.profile(db, slug=f"in/p{h._ids.__next__()}"), company="Acme Training", **kw)
    return prospects.ensure_for_enquiry(db, e.id)


def test_move_stage_any_direction_sets_app_source_and_records_events(db) -> None:
    v = _prospect(db)
    out = prospects.move_stage(db, v.id, "contacted", actor=Actor("Priya", None), now=h.T0)
    assert out.stage == "contacted" and out.contacted_at == h.T0 and out.next_follow_up_on == h.day(3)
    assert db.scalar(select(func.count(FollowUp.id)).where(FollowUp.prospect_id == v.id)) == 3
    assert prospects.move_stage(db, v.id, "follow_up_2", now=h.T0).stage == "follow_up_2"
    back = prospects.move_stage(db, v.id, "contacted", now=h.T0)
    assert back.stage == "contacted"
    row = db.get(Prospect, v.id)
    assert row.stage_source == "app"
    changes = [t for t in prospects.get_prospect(db, v.id).timeline if t.kind == "stage_change"]
    assert [(c.data["from"], c.data["to"]) for c in changes][:3] == [("follow_up_2", "contacted"), ("contacted", "follow_up_2"), ("new", "contacted")]


def test_moving_to_the_same_stage_is_a_noop(db) -> None:
    v = _prospect(db)
    prospects.move_stage(db, v.id, "contacted", now=h.T0)
    n = db.scalar(select(func.count(TimelineEvent.id)))
    prospects.move_stage(db, v.id, "contacted", now=h.T0)
    assert db.scalar(select(func.count(TimelineEvent.id))) == n


def test_invalid_stage_and_closed_prospect_are_refused(db) -> None:
    v = _prospect(db)
    with pytest.raises(DomainError) as ei:
        prospects.move_stage(db, v.id, "won")
    assert ei.value.code == "stage_invalid"
    prospects.close(db, v.id, "no_response")
    with pytest.raises(DomainError) as ei2:
        prospects.move_stage(db, v.id, "contacted")
    assert ei2.value.code == "prospect_closed" and ei2.value.http_status == 409


def test_moving_forward_completes_the_earlier_reminders(db) -> None:
    v = _prospect(db)
    prospects.mark_contacted(db, v.id, when=h.T0, now=h.T0)
    prospects.move_stage(db, v.id, "follow_up_2", now=h.T0 + timedelta(days=8))
    rows = {f.kind: f for f in db.scalars(select(FollowUp).where(FollowUp.prospect_id == v.id))}
    assert rows["follow_up_1"].done_at and rows["follow_up_2"].done_at and rows["final_follow_up"].done_at is None
    assert prospects.get_prospect(db, v.id).next_follow_up_on == h.day(14)


def test_mark_contacted_uses_when_and_is_idempotent(db) -> None:
    v = _prospect(db)
    out = prospects.mark_contacted(db, v.id, when=h.T0, channel="phone", now=h.T0 + timedelta(hours=2))
    assert out.stage == "contacted" and out.contacted_at == h.T0 and not out.contacted_estimated
    prospects.mark_contacted(db, v.id, when=h.T0, now=h.T0)
    assert db.scalar(select(func.count(FollowUp.id)).where(FollowUp.prospect_id == v.id)) == 3


def test_record_response_cancels_reminders_and_is_logged(db) -> None:
    v = _prospect(db)
    prospects.mark_contacted(db, v.id, when=h.T0, now=h.T0)
    out = prospects.record_response(db, v.id, note="called back", now=h.T0 + timedelta(days=1))
    assert out.responded_at == h.T0 + timedelta(days=1) and out.next_follow_up_on is None
    assert all(f.cancel_reason == "response_received" for f in db.scalars(select(FollowUp).where(FollowUp.prospect_id == v.id)))
    assert "reply_received" in kinds(db, v.id) and "follow_up_cancelled" in kinds(db, v.id)
    # the first response time sticks
    again = prospects.record_response(db, v.id, now=h.T0 + timedelta(days=5))
    assert again.responded_at == h.T0 + timedelta(days=1)


def test_close_sets_untracked_for_lost_reasons_and_reopen_restores(db) -> None:
    v = _prospect(db)
    prof = db.get(Prospect, v.id).linkedin_profile_id
    out = prospects.close(db, v.id, "not_interested")
    assert out.status == "closed" and out.closed_reason == "not_interested" and out.tracked is False
    assert prospects.close(db, v.id, "x").closed_reason == "not_interested"  # already closed: unchanged
    back = prospects.reopen(db, v.id)
    assert back.status == "open" and back.tracked is True and db.get(LinkedinProfile, prof).tracked is True


def test_close_with_a_neutral_reason_keeps_tracking_and_blank_reason_is_refused(db) -> None:
    v = _prospect(db)
    assert prospects.close(db, v.id, "duplicate").tracked is True
    w = _prospect(db)
    with pytest.raises(DomainError) as ei:
        prospects.close(db, w.id, "  ")
    assert ei.value.code == "reason_invalid"


def test_set_tracked_toggles_and_needs_a_profile(db) -> None:
    v = _prospect(db)
    assert prospects.set_tracked(db, v.id, False).tracked is False
    assert "tracked_changed" in kinds(db, v.id)
    manual = prospects.create_manual(db, name="Solo Trainer")
    with pytest.raises(DomainError) as ei:
        prospects.set_tracked(db, manual.id, True)
    assert ei.value.code == "tracked_unavailable"


def test_update_name_notes_company(db) -> None:
    v = _prospect(db)
    out = prospects.update(db, v.id, name="Acme Corp", notes="call after Diwali", company="Acme Corp")
    assert out.name == "Acme Corp" and out.company_name == "Acme Corp"
    assert prospects.get_prospect(db, v.id).notes == "call after Diwali"
    with pytest.raises(DomainError):
        prospects.update(db, v.id, name=" ")
    assert prospects.update(db, v.id, company=None).company_id is None


def test_create_manual_and_adoption(db) -> None:
    a = prospects.create_manual(db, name="Rahul Verma", company="Verma Learning", email="rahul@verma.example",
                                linkedin_url="https://www.linkedin.com/in/rahul-verma/", notes="met at a summit")
    assert a.stage == "new" and a.source == "manual" and a.tracked is True and a.contact_id
    b = prospects.create_manual(db, name="R. Verma", email="rahul@verma.example")
    c = prospects.create_manual(db, name="Rahul", linkedin_url="linkedin.com/in/Rahul-Verma")
    assert a.id == b.id == c.id and db.scalar(select(func.count(Prospect.id))) == 1
    with pytest.raises(DomainError) as ei:
        prospects.create_manual(db, name="  ")
    assert ei.value.code == "prospect_invalid"


def test_list_prospects_filters_and_paging(db) -> None:
    a = _prospect(db)
    b = prospects.create_manual(db, name="Zed Corp", company="Zed")
    prospects.move_stage(db, a.id, "contacted", now=h.T0)
    assert {p.id for p in prospects.list_prospects(db).items} == {a.id, b.id}
    assert [p.id for p in prospects.list_prospects(db, stage="contacted").items] == [a.id]
    assert [p.id for p in prospects.list_prospects(db, q="zed").items] == [b.id]
    assert [p.id for p in prospects.list_prospects(db, q="acme training").items] == [a.id]
    assert prospects.list_prospects(db, source="manual").total == 1
    prospects.close(db, b.id, "duplicate")
    assert prospects.list_prospects(db, status="closed").total == 1 and prospects.list_prospects(db, status="all").total == 2
    page = prospects.list_prospects(db, status="all", page=2, page_size=1)
    assert page.page == 2 and len(page.items) == 1 and page.total == 2
    assert prospects.list_prospects(db, q="100%_").total == 0  # LIKE wildcards are escaped


def test_list_filters_by_flag_and_due(db) -> None:
    v = _prospect(db)
    prospects.mark_contacted(db, v.id, when=h.T0, now=h.T0)
    flags.set_flag(db, "prospect", v.id, "repeat_poster", detail={"count": 3})
    assert prospects.list_prospects(db, flag="repeat_poster").total == 1
    assert prospects.list_prospects(db, flag="no_follow_up").total == 0
    db.get(FollowUp, db.scalar(select(FollowUp.id).where(FollowUp.prospect_id == v.id, FollowUp.kind == "follow_up_1"))).due_on = h.day(-400)
    db.flush()
    assert prospects.list_prospects(db, due="overdue").total == 1
    assert prospects.list_prospects(db, due="today").total == 0


def test_get_prospect_unknown_is_404(db) -> None:
    with pytest.raises(DomainError) as ei:
        prospects.get_prospect(db, 424242)
    assert ei.value.http_status == 404


def test_resolve_for_message_paths(db) -> None:
    m = h.member(db)
    v = _prospect(db, email="who@acme.example")
    e = db.scalar(select(Enquiry).where(Enquiry.prospect_id == v.id))
    by_enquiry = h.sent_message(db, m, enquiry_row=e, to="x@y.example")
    assert prospects.resolve_for_message(db, by_enquiry.id) == v.id
    by_fk = h.sent_message(db, m, to="x@y.example", prospect_id=v.id)
    assert prospects.resolve_for_message(db, by_fk.id) == v.id
    by_addr = h.sent_message(db, m, to="Who <who@acme.example>")
    assert prospects.resolve_for_message(db, by_addr.id) == v.id
    nothing = h.sent_message(db, m, to="stranger@nowhere.example")
    assert prospects.resolve_for_message(db, nothing.id) is None
    assert prospects.resolve_for_message(db, 987654) is None


def test_actor_label_is_recorded_on_events(db) -> None:
    v = _prospect(db)
    m = h.member(db, "Priya")
    prospects.move_stage(db, v.id, "contacted", actor=Actor.of_member(m), now=h.T0)
    ev = db.scalar(select(TimelineEvent).where(TimelineEvent.prospect_id == v.id, TimelineEvent.kind == "stage_change"))
    assert ev.actor == "Priya" and ev.actor_member_id == m.id
    assert SYSTEM.label == "system"
