"""Prospect page follow-ups: planned dates + "Log contact" (channel dropdown) on a new prospect, then the three live
follow-ups with inline date edit and "Mark done" with channel + note; the timeline shows the channel."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.clock import local_today
from sales0.config import get_settings
from sales0.db.engine import session_scope
from sales0.db.models import FollowUp
from sales0.services.crm import prospects
from sales0.web.app import create_app
from tests.integration.web.conftest import HX


@pytest.fixture
def web(db_url: str) -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def _prospect() -> int:
    with session_scope() as db:
        return prospects.create_manual(db, name="Priya Menon", company="Acme Logistics").id


def _rows(pid: int) -> dict[str, FollowUp]:
    with session_scope() as db:
        rows = {f.kind: f for f in db.scalars(select(FollowUp).where(FollowUp.prospect_id == pid))}
        db.expunge_all()
        return rows


def test_new_prospect_shows_planned_dates_and_log_contact_schedules_them(web: TestClient) -> None:
    pid = _prospect()
    today = local_today(get_settings().tz)
    page = web.get(f"/pipeline/prospects/{pid}").text
    assert 'id="log-contact-form"' in page and 'id="fu-planned-note"' in page
    for label in ("WhatsApp", "LinkedIn", "Email", "Call"):
        assert f">{label}</option>" in page
    assert f'name="due_follow_up_1" value="{(today + timedelta(days=3)).isoformat()}"' in page
    assert f'name="due_final_follow_up" value="{(today + timedelta(days=14)).isoformat()}"' in page

    custom = (today + timedelta(days=5)).isoformat()
    resp = web.post(f"/pipeline/prospects/{pid}/contact", headers=HX, data={
        "channel": "linkedin", "note": "Connected and sent a note", "contacted_on": today.isoformat(),
        "due_follow_up_1": custom, "due_follow_up_2": "", "due_final_follow_up": (today + timedelta(days=14)).isoformat(),
    })  # fmt: skip
    assert resp.status_code == 200 and "Contact via LinkedIn logged" in resp.text
    assert 'hx-swap-oob="outerHTML:#follow-ups"' in resp.text and 'id="log-contact-form"' not in resp.text
    rows = _rows(pid)
    assert rows["follow_up_1"].due_on.isoformat() == custom and rows["follow_up_2"].due_on == today + timedelta(days=7)
    page = web.get(f"/pipeline/prospects/{pid}").text
    assert ">Contacted<" in page and 'data-channel="linkedin"' in page and "Connected and sent a note" in page
    assert len(re.findall(r'id="fu-done-\d+"', page)) == 3 and len(re.findall(r'id="fu-date-\d+"', page)) == 3


def test_mark_done_with_channel_and_inline_date_edit(web: TestClient) -> None:
    pid = _prospect()
    web.post(f"/pipeline/prospects/{pid}/contact", headers=HX, data={"channel": "call"})
    rows = _rows(pid)
    new_date = (rows["follow_up_2"].due_on + timedelta(days=2)).isoformat()
    moved = web.post(f"/pipeline/followups/{rows['follow_up_2'].id}/snooze", headers=HX, data={"due_on": new_date})
    assert moved.status_code == 200 and f'value="{new_date}"' in moved.text

    done = web.post(f"/pipeline/followups/{rows['follow_up_1'].id}/done", headers=HX, data={"channel": "whatsapp", "note": "Pinged on WhatsApp"})
    assert done.status_code == 200 and "Follow-up done via WhatsApp." in done.text
    assert "Done via WhatsApp" in done.text and ">Follow-up 1<" in done.text  # the header + list fragments are swapped in
    page = web.get(f"/pipeline/prospects/{pid}").text
    assert ">Follow-up 1</span>" in page or "Follow-up 1" in page.split('id="stage-badge"', 1)[1][:40]
    assert 'data-channel="whatsapp"' in page and "Pinged on WhatsApp" in page

    bad = web.post(f"/pipeline/followups/{rows['follow_up_2'].id}/done", headers=HX, data={"channel": "fax"})
    assert "toast--error" in bad.text
    assert _rows(pid)["follow_up_2"].done_at is None


def test_log_contact_without_channel_is_refused(web: TestClient) -> None:
    pid = _prospect()
    plain = web.post(f"/pipeline/prospects/{pid}/contact", data={"channel": ""}, follow_redirects=False)
    assert plain.status_code == 303
    assert "flash--error" in web.get(f"/pipeline/prospects/{pid}").text and _rows(pid) == {}
