"""Follow-ups record HOW they were done (WhatsApp / LinkedIn / Email / Call), the first contact can be logged by hand
with the agent's own follow-up dates, dates are editable, and every step keeps the stage + timeline consistent."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from sales0.db.models import FollowUp, TimelineEvent
from sales0.errors import DomainError
from sales0.services.crm import followups, prospects
from tests.integration.crm import helpers as h

D = h.T0  # 2026-10-01 09:00 UTC = 14:30 IST (local day 2026-10-01)


def _new(db, name="Acme Channel"):
    e = h.enquiry(db, company=name)
    return prospects.ensure_for_enquiry(db, e.id)


def _rows(db, pid):
    return {f.kind: f for f in db.scalars(select(FollowUp).where(FollowUp.prospect_id == pid))}


def _events(db, pid, kind):
    return list(db.scalars(select(TimelineEvent).where(TimelineEvent.prospect_id == pid, TimelineEvent.kind == kind, TimelineEvent.subject_type == "prospect").order_by(TimelineEvent.id)))


def test_plan_is_contact_day_plus_settings_offsets() -> None:
    assert followups.plan(date(2026, 10, 1)) == [
        ("follow_up_1", "Follow-up 1", date(2026, 10, 4)), ("follow_up_2", "Follow-up 2", date(2026, 10, 8)),
        ("final_follow_up", "Final follow-up", date(2026, 10, 15)),
    ]  # fmt: skip


def test_log_contact_moves_new_to_contacted_with_channel_note_and_custom_dates(db) -> None:
    v = _new(db)
    out = prospects.log_contact(
        db, v.id, channel="WhatsApp", note="sent the brochure", now=D,
        due_dates={"follow_up_1": date(2026, 10, 3), "follow_up_2": None, "final_follow_up": date(2026, 10, 20)},
    )  # fmt: skip
    assert out.stage == "contacted"
    rows = _rows(db, v.id)
    assert {k: f.due_on for k, f in rows.items()} == {
        "follow_up_1": date(2026, 10, 3), "follow_up_2": date(2026, 10, 8), "final_follow_up": date(2026, 10, 20)}
    stage = _events(db, v.id, "stage_change")[-1]
    assert stage.data["channel"] == "whatsapp" and stage.data["note"] == "sent the brochure" and "sent the brochure" in stage.body
    with pytest.raises(DomainError) as exc:
        prospects.log_contact(db, v.id, channel="call", now=D)
    assert exc.value.code == "already_contacted"


def test_log_contact_validates_channel_and_dates(db) -> None:
    v = _new(db)
    for kw, code in (
        ({"channel": ""}, "followup_invalid"),
        ({"channel": "pigeon"}, "followup_invalid"),
        ({"channel": "call", "contacted_on": date(2026, 10, 5)}, "contact_invalid"),  # the future
        ({"channel": "call", "due_dates": {"follow_up_1": date(2026, 9, 1)}}, "followup_invalid"),  # before contact
    ):
        with pytest.raises(DomainError) as exc:
            prospects.log_contact(db, v.id, now=D, **kw)
        assert exc.value.code == code, kw
    assert _rows(db, v.id) == {}


def test_back_dated_contact_counts_offsets_from_that_day(db) -> None:
    v = _new(db)
    prospects.log_contact(db, v.id, channel="linkedin", contacted_on=date(2026, 9, 28), now=D)
    assert _rows(db, v.id)["follow_up_1"].due_on == date(2026, 10, 1)
    assert prospects.get_prospect(db, v.id).contacted_at.date() == date(2026, 9, 28)


def test_mark_done_with_channel_moves_the_stage_and_records_channel_and_note(db) -> None:
    v = _new(db)
    prospects.log_contact(db, v.id, channel="call", now=D)
    fu1 = _rows(db, v.id)["follow_up_1"]
    view = followups.complete(db, fu1.id, channel="LinkedIn", note="InMail sent", now=D + timedelta(days=3))
    assert view.done and view.channel == "linkedin" and view.channel_label == "LinkedIn"
    assert fu1.channel == "linkedin" and fu1.note == "InMail sent"
    assert prospects.get_prospect(db, v.id).stage == "follow_up_1"
    done = _events(db, v.id, "follow_up_done")[-1]
    assert done.data["channel"] == "linkedin" and done.body == "Follow-up 1 done via LinkedIn: InMail sent"
    stage = _events(db, v.id, "stage_change")[-1]
    assert stage.data == {**stage.data, "from": "contacted", "to": "follow_up_1", "channel": "linkedin"}
    with pytest.raises(DomainError):
        followups.complete(db, _rows(db, v.id)["follow_up_2"].id, channel="fax")


def test_reschedule_changes_the_date_and_next_follow_up(db) -> None:
    v = _new(db)
    prospects.log_contact(db, v.id, channel="email", now=D)
    fu1 = _rows(db, v.id)["follow_up_1"]
    followups.snooze(db, fu1.id, date(2026, 10, 2), now=D)  # earlier is allowed too: the agent decides
    assert prospects.get_prospect(db, v.id).next_follow_up_on == date(2026, 10, 2)
    assert "Follow-up 1 moved from 2026-10-04 to 2026-10-02" in _events(db, v.id, "follow_up_added")[-1].body
    n = len(_events(db, v.id, "follow_up_added"))
    followups.snooze(db, fu1.id, date(2026, 10, 2), now=D)  # same date: no-op, no event
    assert len(_events(db, v.id, "follow_up_added")) == n


def test_follow_up_views_show_done_rows_with_channel(db) -> None:
    v = _new(db)
    prospects.log_contact(db, v.id, channel="call", now=D)
    followups.complete(db, _rows(db, v.id)["follow_up_1"].id, channel="whatsapp", now=D)
    views = followups.for_prospect(db, v.id, today=date(2026, 10, 1))
    assert [(f.kind, f.done, f.channel_label) for f in views] == [
        ("follow_up_2", False, None), ("final_follow_up", False, None), ("follow_up_1", True, "WhatsApp")]


def test_gmail_send_marks_the_follow_up_done_via_email(db, installed) -> None:
    from sales0.services import events

    m = h.member(db)
    e = h.enquiry(db, company="Mail Co")
    v = prospects.ensure_for_enquiry(db, e.id)
    first = h.sent_message(db, m, enquiry_row=e, sent_at=D)
    events.publish(db, events.MessageSent(outreach_message_id=first.id, member_id=m.id, to_addrs=("asha@acme.example",), sent_at=D))
    assert prospects.get_prospect(db, v.id).stage == "contacted"
    later = D + timedelta(days=3)
    second = h.sent_message(db, m, enquiry_row=e, sent_at=later)
    events.publish(db, events.MessageSent(outreach_message_id=second.id, member_id=m.id, to_addrs=("asha@acme.example",), sent_at=later))
    fu1 = _rows(db, v.id)["follow_up_1"]
    assert fu1.done_at is not None and fu1.channel == "email"
    assert prospects.get_prospect(db, v.id).stage == "follow_up_1"
