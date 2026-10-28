"""Gmail draft-to-self: DRAFT ONLY (never send_draft), connection and identity checks."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from sales0.errors import DomainError
from sales0.integrations.gmail.client import FakeGmailClient
from sales0.services import digest as dg
from sales0.services.team import members
from tests.integration.digest.conftest import NOW, Team, add_enquiry, hours_ago


def test_draft_is_created_to_self_and_never_sent(db: Session, team: Team) -> None:
    add_enquiry(db, added=hours_ago(1), company="Acme Logistics")
    view = dg.generate_digest(db, now=NOW, with_summary=False)
    draft_id = dg.create_gmail_draft_to_self(db, view.id, team.ann)
    client = team.gmail[team.ann.id]
    names = [c[0] for c in client.calls]
    assert names.count("create_draft") == 1 and "send_draft" not in names and client.sent == []
    call = next(c for c in client.calls if c[0] == "create_draft")
    assert call[1][0] == ("ann@team.example",) and call[1][1] == () and call[1][2] == "sales0 daily digest - 2026-10-28"
    body = client.get_draft(draft_id).content.text_body
    assert "Acme Logistics" in body and "NEW RELEVANT ENQUIRIES" in body
    stored = dg.get_digest(db, view.id)
    assert stored.gmail_draft_id == draft_id and stored.gmail_draft_member_id == team.ann.id
    other = [c for mid, c in team.gmail.items() if mid != team.ann.id]
    assert all(not any(n == "create_draft" for n, _ in o.calls) for o in other)


def test_not_connected_member_gets_a_clear_error(db: Session, team: Team) -> None:
    view = dg.generate_digest(db, now=NOW, with_summary=False)
    cy = members.add_member(db, "Cy Lee", "cy@team.example")  # never connected Gmail
    db.commit()
    with pytest.raises(DomainError) as exc:
        dg.create_gmail_draft_to_self(db, view.id, cy)
    assert exc.value.code == "gmail_not_connected" and exc.value.http_status == 409
    assert all(not any(c[0] == "create_draft" for c in g.calls) for g in team.gmail.values())
    assert dg.get_digest(db, view.id).gmail_draft_id is None


def test_wrong_gmail_account_is_refused_without_a_draft(db: Session, team: Team) -> None:
    view = dg.generate_digest(db, now=NOW, with_summary=False)
    team.gmail[team.ann.id] = FakeGmailClient(email="someone.else@example.com")
    with pytest.raises(DomainError) as exc:
        dg.create_gmail_draft_to_self(db, view.id, team.ann)
    assert exc.value.code == "sender_mismatch"
    assert not any(c[0] == "create_draft" for c in team.gmail[team.ann.id].calls)


def test_unknown_digest_is_not_found(db: Session, team: Team) -> None:
    with pytest.raises(DomainError) as exc:
        dg.create_gmail_draft_to_self(db, 404, team.ann)
    assert exc.value.http_status == 404
