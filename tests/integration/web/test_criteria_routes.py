"""Inbox feedback A3 through the real app (open mode = the acting admin): the Criteria page, rule edits, and the
discard form's own reason + "save as rule". Member permissions live in tests/integration/members/."""

from __future__ import annotations

from html import unescape

from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.agent.criteria import DEFAULT_EXCLUDE, DEFAULT_INCLUDE
from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, TriageCriterion, TriageDecision
from sales0.services import criteria
from tests.integration.web.conftest import HX, make_enquiry, triaged


def _rule(text: str) -> TriageCriterion | None:
    with session_scope() as db:
        row = db.scalar(select(TriageCriterion).where(TriageCriterion.text == text))
        if row is not None:
            db.expunge(row)
        return row


def test_page_lists_the_seeded_rules_breakdown_and_controls(client: TestClient) -> None:
    html = unescape(client.get("/criteria").text)
    assert 'id="include-list"' in html and 'id="exclude-list"' in html and 'id="discard-reasons"' in html
    assert DEFAULT_INCLUDE[0] in html and DEFAULT_EXCLUDE[-1] in html
    assert "version 1" in html and 'id="new-rule"' in html and "Disable" in html
    assert 'href="/criteria"' in html  # in the nav


def test_add_disable_move_and_edit_a_rule(client: TestClient) -> None:
    r = client.post("/criteria", data={"kind": "exclude", "text": "Requests for free guest lectures at colleges"},
                    follow_redirects=False)
    assert r.status_code == 303
    rule = _rule("Requests for free guest lectures at colleges")
    assert rule is not None and rule.active and rule.source == "team"
    assert client.post(f"/criteria/{rule.id}/move", data={"direction": "up"}, follow_redirects=False).status_code == 303
    assert client.post(f"/criteria/{rule.id}/active", data={"active": "false"}, follow_redirects=False).status_code == 303
    assert _rule("Requests for free guest lectures at colleges").active is False  # type: ignore[union-attr]
    client.post(f"/criteria/{rule.id}", data={"text": "Requests for free guest lectures or student talks", "kind": "exclude"})
    assert _rule("Requests for free guest lectures or student talks") is not None
    with session_scope() as db:
        assert criteria.current_version(db) == 5
    page = unescape(client.get("/criteria").text)
    assert "Requests for free guest lectures or student talks" in page and "version 5" in page


def test_invalid_rule_shows_an_error_flash(client: TestClient) -> None:
    r = client.post("/criteria", data={"kind": "exclude", "text": "no"}, follow_redirects=True)
    assert r.status_code == 200 and "at least 5 characters" in unescape(r.text)


def test_card_offers_own_reason_and_save_as_rule_to_admins(client: TestClient) -> None:
    eid = make_enquiry("P1", **triaged("genuine", "Buyer"))
    card = client.get("/inbox").text
    start = card.index(f'<article id="enquiry-{eid}"')
    card = card[start : card.index("</article>", start)]
    assert 'name="custom_reason"' in card and 'name="save_rule"' in card


def test_discard_with_own_reason_teaches_fewshot_and_can_become_a_rule(client: TestClient) -> None:
    eid = make_enquiry("P1", **triaged("genuine", "Buyer"))
    r = client.post(
        "/inbox/decide",
        data={"enquiry_ids": eid, "decision": "discard", "discard_reason": "Not a requirement",
              "custom_reason": "  Agency reselling   our workshop to its client ", "save_rule": "1"},
        headers=HX,
    )
    assert r.status_code == 200 and "saved as a triage rule" in r.text
    with session_scope() as db:
        decision = db.scalar(select(TriageDecision).where(TriageDecision.enquiry_id == eid))
        assert decision is not None and decision.reason == "Agency reselling our workshop to its client"
        e = db.get(Enquiry, eid)
        assert e is not None and e.discard_reason == "Agency reselling our workshop to its client"
    rule = _rule("Agency reselling our workshop to its client")
    assert rule is not None and (rule.kind, rule.source, rule.from_enquiry_id) == ("exclude", "discard", eid)
    page = unescape(client.get("/criteria").text)
    assert "From a discard" in page and "Agency reselling our workshop to its client" in page


def test_preset_reason_still_works_and_too_long_is_refused(client: TestClient) -> None:
    eid = make_enquiry("P1", **triaged("genuine", "Buyer"))
    too_long = client.post("/inbox/decide", data={"enquiry_ids": eid, "decision": "discard", "custom_reason": "x" * 201},
                           headers=HX)
    assert too_long.headers.get("HX-Retarget") == "#toasts"
    client.post("/inbox/decide", data={"enquiry_ids": eid, "decision": "discard", "discard_reason": "No response"}, headers=HX)
    with session_scope() as db:
        assert db.scalar(select(TriageDecision.reason).where(TriageDecision.enquiry_id == eid)) == "No response"
