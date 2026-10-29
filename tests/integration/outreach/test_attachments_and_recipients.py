"""Attachments are never silently dropped (DB-stored deck, visible on draft / approval / sent record, warnings when the
offering has no deck), and Compose offers the prospect's contacts as recipients (suppressed ones disabled).
Gmail is the FakeGmailClient: nothing leaves the process."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from sales0.db.engine import get_sessionmaker
from sales0.db.models import ContactEmail, Deck, OutreachEvent, OutreachMessage
from sales0.llm import set_llm_client
from sales0.services import suppressions
from sales0.services.crm import contacts, prospects
from sales0.services.outreach import decks, recipients
from sales0.web.app import create_app
from tests.integration.outreach.conftest import PDF, Team
from tests.support.fake_llm import FakeLLMClient

GOOD = {
    "subject": "AI workshops for Acme Logistics",
    "body": "Hello Priya,\n\nI saw Acme Logistics wants a GenAI workshop. We can help.\n\nBest regards,\nAnn Rao",
}


@pytest.fixture
def web(team: Team) -> Iterator[TestClient]:
    set_llm_client(FakeLLMClient(responder=lambda req: GOOD))
    with TestClient(create_app()) as c:
        assert c.post("/team/acting", data={"member_id": team.ann.id, "next": "/outreach"}, follow_redirects=False).status_code == 303
        yield c


def _draft(web: TestClient, **over: str) -> int:
    data = {"to": "priya@acme.example", "contact_name": "Priya", "company": "Acme Logistics", "requirement": "GenAI workshop",
            "offering_id": "", "template_id": "", "deck_id": ""}  # fmt: skip
    data.update(over)
    resp = web.post("/outreach/draft", data=data, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return int(resp.headers["location"].rsplit("/", 1)[1])


# ------------------------------------------------------------------ C1: attachments
def test_mapped_deck_is_preselected_shown_everywhere_and_actually_sent(web: TestClient, team: Team, db: Session, offering) -> None:
    deck = decks.upload_deck(db, filename="Workshops.pdf", data=PDF, offering_id=offering.id)
    db.commit()
    html = web.get(f"/outreach/compose/defaults?offering_id={offering.id}").text
    assert re.search(rf'<option value="{deck.id}" selected', html) and 'data-deck-note="ok"' in html

    msg_id = _draft(web, offering_id=str(offering.id))  # deck_id "" -> the offering's deck
    page = web.get(f"/outreach/messages/{msg_id}").text
    assert 'id="approve-attachment"' in page and "Workshops.pdf (" in page and 'id="no-attachment-warning"' not in page
    web.post(f"/outreach/messages/{msg_id}/approve")
    assert 'id="approved-attachment">Workshops.pdf (' in web.get(f"/outreach/messages/{msg_id}").text
    web.post(f"/outreach/messages/{msg_id}/send")
    sent = team.gmail[team.ann.id].sent
    assert len(sent) == 1 and b"Workshops.pdf" in sent[0]["raw"] and b"application/pdf" in sent[0]["raw"]
    page = web.get(f"/outreach/messages/{msg_id}").text
    assert 'id="sent-attachment">Workshops.pdf (' in page
    with get_sessionmaker()() as s:
        details = {e.kind: e.detail or "" for e in s.query(OutreachEvent).filter_by(message_id=msg_id)}
    assert "attachment=Workshops.pdf" in details["approved"] and "attachment=Workshops.pdf" in details["sent"]


def test_offering_without_a_deck_warns_instead_of_silently_sending_without_one(web: TestClient, db: Session, offering) -> None:
    other = decks.upload_deck(db, filename="Other.pdf", data=PDF, offering_id=offering.id + 1000)  # mapped elsewhere
    db.commit()
    html = web.get(f"/outreach/compose/defaults?offering_id={offering.id}").text
    assert 'data-deck-note="warn"' in html and "WITHOUT an attachment" in html and "AI corporate workshops" in html
    assert not re.search(rf'<option value="{other.id}" selected', html)
    msg_id = _draft(web, offering_id=str(offering.id))
    page = web.get(f"/outreach/messages/{msg_id}").text
    assert 'id="no-attachment-warning"' in page and "none - no deck will be attached" in page


def test_edit_without_a_deck_field_keeps_the_attachment(web: TestClient, team: Team, db: Session, deck) -> None:
    msg_id = _draft(web, deck_id=str(deck.id))
    web.post(f"/outreach/messages/{msg_id}/edit", data={"to": "priya@acme.example", "cc": "", "subject": "New subject", "body": GOOD["body"]})
    with get_sessionmaker()() as s:
        msg = s.get(OutreachMessage, msg_id)
        assert msg.subject == "New subject" and msg.deck_id == deck.id  # used to default to "none" and detach it
    web.post(f"/outreach/messages/{msg_id}/edit", data={"to": "priya@acme.example", "cc": "", "subject": "New subject", "body": GOOD["body"], "deck_id": "none"})
    with get_sessionmaker()() as s:
        assert s.get(OutreachMessage, msg_id).deck_id is None  # an explicit "none" still removes it


def test_missing_deck_file_is_flagged_and_blocks_approval_with_upload_again(web: TestClient, team: Team, db: Session, deck) -> None:
    msg_id = _draft(web, deck_id=str(deck.id))
    from sales0.config import get_settings

    (get_settings().data_path / deck.stored_path).unlink()  # the file vanished (as on the hosted /tmp)
    page = web.get(f"/outreach/messages/{msg_id}").text
    assert 'id="attachment-missing"' in page
    db.get(Deck, deck.id).content_missing = True
    db.commit()
    page = web.get(f"/outreach/messages/{msg_id}").text
    assert 'id="attachment-missing"' in page and "upload it again" in page
    resp = web.post(f"/outreach/messages/{msg_id}/approve", headers={"Accept": "application/json"})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "deck_missing"
    assert "File missing - upload again" in web.get("/outreach/decks").text
    assert web.get("/outreach/compose/defaults").text.count("file missing, upload again") == 1  # listed but disabled
    assert team.gmail[team.ann.id].sent == []


def test_deck_download_is_served_from_storage(web: TestClient, team: Team, db: Session, deck) -> None:
    resp = web.get(f"/outreach/decks/{deck.id}/file")
    assert resp.status_code == 200 and resp.content == PDF


# ------------------------------------------------------------------ C3: recipients
def _prospect_with_contacts(db: Session) -> int:
    v = prospects.create_manual(db, name="Priya Menon", company="Acme Logistics", email="priya@acme.example")
    p = prospects.get_prospect(db, v.id)
    primary = p.contact_id or p.contacts[0].id
    db.add(ContactEmail(contact_id=primary, email="priya.personal@mail.example"))
    other, _ = contacts.upsert_row(db, full_name="Ravi Kumar", email="ravi@acme.example", company_id=p.company_id)
    blocked, _ = contacts.upsert_row(db, full_name="Old Boss", email="boss@acme.example", company_id=p.company_id)
    suppressions.add(db, "boss@acme.example", "unsubscribe")
    db.commit()
    return v.id


def test_recipient_options_order_multiple_emails_and_suppression(db: Session, team: Team) -> None:
    pid = _prospect_with_contacts(db)
    opts = recipients.for_prospect(db, pid)
    assert [o.email for o in opts] == ["priya@acme.example", "priya.personal@mail.example", "boss@acme.example", "ravi@acme.example"]
    assert opts[0].primary and opts[0].label == "Priya Menon - priya@acme.example (primary)"
    assert [o.email for o in opts if o.suppressed] == ["boss@acme.example"]
    assert [o.email for o in recipients.search(db, "ravi")] == ["ravi@acme.example"]
    assert recipients.search(db, "  ") == []


def test_compose_lists_contacts_preselects_the_prefill_and_disables_suppressed(web: TestClient, db: Session) -> None:
    pid = _prospect_with_contacts(db)
    html = web.get(f"/outreach/compose?prospect_id={pid}").text
    assert 'id="recipient-pick"' in html and 'data-fill-recipient="to"' in html
    assert re.search(r'<option value="priya@acme.example" data-email="priya@acme.example" data-name="Priya Menon" data-contact-id="\d+" selected>', html)
    assert re.search(r'value="boss@acme.example"[^>]* disabled>Old Boss - boss@acme.example - suppressed', html)
    assert "ravi@acme.example" in html and 'id="to" name="to"' in html  # free text stays
    found = web.get("/outreach/compose/contacts?contact_q=Ravi").text
    assert "Ravi Kumar - ravi@acme.example" in found and "priya" not in found
    assert 'No contact matches "zzz"' in web.get("/outreach/compose/contacts?contact_q=zzz").text


def test_compose_without_a_prospect_has_an_empty_picker_and_search(web: TestClient) -> None:
    html = web.get("/outreach/compose").text
    assert "No saved contacts" in html and 'hx-get="/outreach/compose/contacts"' in html
