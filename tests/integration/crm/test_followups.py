"""Reminders with a frozen clock: schedule maths, Due today / Overdue, complete, snooze, custom, cancel."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from sales0.config import reset_settings
from sales0.db.models import FollowUp, Prospect
from sales0.errors import DomainError
from sales0.services.crm import flags, followups, leads, prospects
from tests.integration.crm import helpers as h

D = h.T0  # 2026-10-01 09:00 UTC = 14:30 IST


def _contacted(db, name="Acme Training"):
    e = h.enquiry(db, profile_row=h.profile(db, slug=f"in/f{h._ids.__next__()}"), company=name)
    v = prospects.ensure_for_enquiry(db, e.id)
    prospects.move_stage(db, v.id, "contacted", now=D)
    return v


def _rows(db, pid):
    return {f.kind: f for f in db.scalars(select(FollowUp).where(FollowUp.prospect_id == pid))}


def test_moving_to_contacted_schedules_3_7_14_days(db) -> None:
    v = _contacted(db)
    rows = _rows(db, v.id)
    assert {k: f.due_on for k, f in rows.items()} == {
        "follow_up_1": date(2026, 10, 4), "follow_up_2": date(2026, 10, 8), "final_follow_up": date(2026, 10, 15)}
    assert all(f.auto for f in rows.values())
    assert prospects.get_prospect(db, v.id).next_follow_up_on == date(2026, 10, 4)


def test_settings_drive_the_offsets(db, monkeypatch) -> None:
    monkeypatch.setenv("SALES0_FOLLOWUP_DAYS_1", "2")
    reset_settings()
    v = _contacted(db)
    assert _rows(db, v.id)["follow_up_1"].due_on == date(2026, 10, 3)


def test_offsets_count_from_the_local_contact_date(db) -> None:
    # 20:00 UTC on Oct 1 is 01:30 IST on Oct 2 -> the contact date is Oct 2 in SALES0_TIMEZONE
    e = h.enquiry(db, company="Late Co")
    v = prospects.ensure_for_enquiry(db, e.id)
    late = D.replace(hour=20)
    prospects.mark_contacted(db, v.id, when=late, now=late)
    assert _rows(db, v.id)["follow_up_1"].due_on == date(2026, 10, 5)


def test_schedule_is_idempotent_and_respects_no_follow_up(db) -> None:
    v = _contacted(db)
    assert followups.schedule_for_contact(db, v.id, D, now=D) == []
    assert len(_rows(db, v.id)) == 3
    w = h.enquiry(db, company="Quiet Co")
    pw = prospects.ensure_for_enquiry(db, w.id)
    flags.set_flag(db, "prospect", pw.id, "no_follow_up", source="user")
    assert followups.schedule_for_contact(db, pw.id, D, now=D) == []


def test_due_today_overdue_and_count_with_a_frozen_clock(db) -> None:
    _contacted(db)
    p3 = date(2026, 10, 4)
    panel = followups.list_due(db, today=p3)
    assert [f.kind for f in panel.due_today] == ["follow_up_1"] and panel.overdue == []
    assert panel.due_today[0].who == "Acme Training" and panel.due_today[0].kind_label == "Follow-up 1"
    assert followups.due_count(db, today=p3) == 1
    assert followups.list_due(db, today=date(2026, 10, 3)).due_today == []
    p4 = followups.list_due(db, today=date(2026, 10, 5))
    assert p4.due_today == [] and [f.kind for f in p4.overdue] == ["follow_up_1"] and p4.overdue[0].overdue is True
    assert followups.due_count(db, today=date(2026, 10, 9)) == 2  # fu1 + fu2 overdue


def test_complete_moves_the_prospect_to_the_reminder_stage(db) -> None:
    v = _contacted(db)
    fu1 = _rows(db, v.id)["follow_up_1"]
    out = followups.complete(db, fu1.id, note="emailed again", now=D + timedelta(days=4))
    assert out.kind == "follow_up_1"
    assert prospects.get_prospect(db, v.id).stage == "follow_up_1"
    assert followups.list_due(db, today=date(2026, 10, 4)).due_today == []
    assert prospects.get_prospect(db, v.id).next_follow_up_on == date(2026, 10, 8)
    followups.complete(db, fu1.id)  # idempotent
    fu2 = _rows(db, v.id)["follow_up_2"]
    followups.complete(db, fu2.id, now=D + timedelta(days=8))
    followups.complete(db, fu1.id)
    assert prospects.get_prospect(db, v.id).stage == "follow_up_2"  # never moved back


def test_completing_a_later_reminder_completes_the_earlier_ones(db) -> None:
    v = _contacted(db)
    followups.complete(db, _rows(db, v.id)["final_follow_up"].id, now=D)
    rows = _rows(db, v.id)
    assert all(f.done_at for f in rows.values()) and prospects.get_prospect(db, v.id).stage == "final_follow_up"
    assert prospects.get_prospect(db, v.id).next_follow_up_on is None


def test_snooze_and_custom_and_validation(db) -> None:
    v = _contacted(db)
    fu1 = _rows(db, v.id)["follow_up_1"]
    out = followups.snooze(db, fu1.id, date(2026, 10, 20))
    assert out.due_on == date(2026, 10, 20)
    assert prospects.get_prospect(db, v.id).next_follow_up_on == date(2026, 10, 8)
    with pytest.raises(DomainError) as ei:
        followups.snooze(db, fu1.id, "2026-10-21")  # type: ignore[arg-type]
    assert ei.value.code == "followup_invalid"
    c = followups.add_custom(db, prospect_id=v.id, due_on=date(2026, 10, 2), note="  call Asha  ")
    assert c.kind == "custom" and c.note == "call Asha" and c.who == "Acme Training"
    assert prospects.get_prospect(db, v.id).next_follow_up_on == date(2026, 10, 2)
    with pytest.raises(DomainError):
        followups.add_custom(db, prospect_id=v.id, due_on=date(1999, 1, 1))
    with pytest.raises(ValueError):
        followups.add_custom(db, due_on=date(2026, 10, 2))
    with pytest.raises(DomainError) as nf:
        followups.add_custom(db, prospect_id=99999, due_on=date(2026, 10, 2))
    assert nf.value.http_status == 404
    # completing a custom reminder does not change the stage
    followups.complete(db, c.id)
    assert prospects.get_prospect(db, v.id).stage == "contacted"
    # a done reminder cannot be snoozed
    with pytest.raises(DomainError):
        followups.snooze(db, c.id, date(2026, 10, 30))


def test_lead_reminders_appear_in_the_panel(db) -> None:
    v = _contacted(db)
    prospects.record_response(db, v.id, now=D)
    ld = leads.convert(db, v.id, now=D)
    c = followups.add_custom(db, lead_id=ld.id, due_on=date(2026, 10, 6), note="send proposal")
    assert c.lead_id == ld.id and c.who == "Acme Training"
    assert [f.id for f in followups.list_due(db, today=date(2026, 10, 6)).due_today] == [c.id]
    assert followups.cancel_open(db, lead_id=ld.id, reason="lead_lost") == 1
    assert followups.list_due(db, today=date(2026, 10, 6)).due_today == []


def test_cancel_open_keeps_rows_with_a_reason(db) -> None:
    v = _contacted(db)
    assert followups.cancel_open(db, prospect_id=v.id, reason="manual_stop") == 3
    assert followups.cancel_open(db, prospect_id=v.id, reason="manual_stop") == 0
    assert all(f.cancel_reason == "manual_stop" and f.cancelled_at for f in _rows(db, v.id).values())
    assert prospects.get_prospect(db, v.id).next_follow_up_on is None
    with pytest.raises(ValueError):
        followups.cancel_open(db, reason="x")


def test_no_follow_up_prospects_and_closed_ones_are_excluded_from_due(db) -> None:
    a, b = _contacted(db, "A Co"), _contacted(db, "B Co")
    day4 = date(2026, 10, 4)
    assert followups.due_count(db, today=day4) == 2
    flags.set_flag(db, "prospect", a.id, "no_follow_up", source="user")  # also cancels its reminders
    assert followups.due_count(db, today=day4) == 1
    row = db.get(Prospect, b.id)
    row.status = "closed"
    db.flush()
    assert followups.due_count(db, today=day4) == 0


def test_member_filter(db) -> None:
    v = _contacted(db)
    m = h.member(db)
    assert followups.list_due(db, today=date(2026, 10, 4), member_id=m.id).due_today == []
    db.get(Prospect, v.id).actor_member_id = m.id
    db.flush()
    assert len(followups.list_due(db, today=date(2026, 10, 4), member_id=m.id).due_today) == 1
