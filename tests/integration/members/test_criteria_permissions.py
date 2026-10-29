"""Inbox feedback A3: members can read the Criteria page and discard with their own reason, but only admins change
rules (403 for members; the controls they cannot use are hidden)."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sales0.db.engine import session_scope
from sales0.db.models import TriageCriterion, TriageDecision
from tests.integration.members.conftest import activate, add_member
from tests.integration.web.conftest import make_enquiry, triaged

HX = {"HX-Request": "true"}


def _member(admin: TestClient, make_client: Callable[[], TestClient]) -> TestClient:
    link = add_member(admin, "Mia Member", "mia@acct.example")
    return activate(make_client, link, "mia@acct.example")


def test_member_sees_rules_without_controls_and_gets_403_on_changes(
    admin: TestClient, make_client: Callable[[], TestClient]
) -> None:
    mia = _member(admin, make_client)
    page = mia.get("/criteria")
    assert page.status_code == 200 and 'id="exclude-list"' in page.text
    assert 'id="new-rule"' not in page.text and "/move" not in page.text and "Only an admin" in page.text
    with session_scope() as db:
        rule_id = db.scalar(select(TriageCriterion.id).order_by(TriageCriterion.id).limit(1))
    assert mia.post("/criteria", data={"kind": "exclude", "text": "Members cannot add this"}).status_code == 403
    assert mia.post(f"/criteria/{rule_id}/active", data={"active": "false"}).status_code == 403
    assert mia.post(f"/criteria/{rule_id}/move", data={"direction": "down"}).status_code == 403
    assert admin.post("/criteria", data={"kind": "exclude", "text": "Admins can add this"}).status_code == 303


def test_member_discard_keeps_the_reason_but_save_as_rule_is_admin_only(
    admin: TestClient, make_client: Callable[[], TestClient]
) -> None:
    mia = _member(admin, make_client)
    eid = make_enquiry("P1", **triaged("genuine", "Buyer"))
    card = mia.get("/inbox").text
    assert 'name="custom_reason"' in card and 'name="save_rule"' not in card
    refused = mia.post("/inbox/decide", data={"enquiry_ids": eid, "decision": "discard", "custom_reason": "Reseller",
                                              "save_rule": "1"})
    assert refused.status_code == 403
    ok = mia.post("/inbox/decide", data={"enquiry_ids": eid, "decision": "discard", "custom_reason": "Reseller agency"},
                  headers=HX)
    assert ok.status_code == 200
    with session_scope() as db:
        assert db.scalar(select(TriageDecision.reason).where(TriageDecision.enquiry_id == eid)) == "Reseller agency"
        assert db.scalar(select(func.count(TriageCriterion.id)).where(TriageCriterion.source == "discard")) == 0
