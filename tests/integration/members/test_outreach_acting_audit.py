"""Security fix #2: an admin approving/sending while "acting as" a member is recorded as "X as Y"."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.db.engine import session_scope
from sales0.db.models import OutreachEvent, OutreachMessage, TeamMember
from sales0.services.outreach._shared import split_acted_by
from tests.integration.members.conftest import activate, add_member
from tests.integration.outreach.conftest import make_message


def test_admin_approving_as_member_records_both_names(admin: TestClient, make_client: Callable[[], TestClient]) -> None:
    activate(make_client, add_member(admin, "Meera Member", "meera@acct.example"), "meera@acct.example")
    with session_scope() as db:
        meera = db.scalar(select(TeamMember).where(TeamMember.email == "meera@acct.example"))
        boss = db.scalar(select(TeamMember).where(TeamMember.role == "admin"))
        meera_id, boss_id = meera.id, boss.id
        msg_id = make_message(db, meera).id
    assert admin.post("/team/acting", data={"member_id": meera_id, "next": "/outreach"}).status_code == 303

    assert admin.post(f"/outreach/messages/{msg_id}/approve").status_code == 303
    with session_scope() as db:
        ev = db.scalars(select(OutreachEvent).where(OutreachEvent.message_id == msg_id, OutreachEvent.kind == "approved")).one()
        assert ev.member_id == meera_id  # credit stays with the owner ...
        detail, by = split_acted_by(ev.detail)
        assert by == boss_id and detail and detail.startswith("sha256=")  # ... the real approver is recorded
        assert db.get(OutreachMessage, msg_id).approved_by_member_id == meera_id
    html = admin.get(f"/outreach/messages/{msg_id}").text
    assert "Boss Admin as Meera Member" in html and "by_member_id" not in html


def test_member_acting_for_themselves_has_no_audit_suffix(admin: TestClient, make_client: Callable[[], TestClient]) -> None:
    meera = activate(make_client, add_member(admin, "Meera Member", "meera@acct.example"), "meera@acct.example")
    with session_scope() as db:
        mid = db.scalar(select(TeamMember.id).where(TeamMember.email == "meera@acct.example"))
        msg_id = make_message(db, db.get(TeamMember, mid)).id
    assert meera.post(f"/outreach/messages/{msg_id}/approve").status_code == 303
    with session_scope() as db:
        ev = db.scalars(select(OutreachEvent).where(OutreachEvent.message_id == msg_id, OutreachEvent.kind == "approved")).one()
        assert split_acted_by(ev.detail)[1] is None and "by_member_id" not in (ev.detail or "")
