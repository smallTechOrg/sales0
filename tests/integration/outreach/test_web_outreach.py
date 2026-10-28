"""Outreach web pages through the real app (TestClient): compose -> draft -> approve -> send
with a FakeGmailClient, permissions, the Send control rule, decks, templates, research."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.clock import utcnow
from sales0.config import reset_settings
from sales0.db.engine import get_sessionmaker, session_scope
from sales0.db.models import CompanyBrief, OutreachEvent, OutreachMessage
from sales0.integrations.gmail.errors import GmailTransientError
from sales0.llm import set_llm_client
from sales0.services.team import connections
from sales0.web.app import create_app
from tests.integration.outreach.conftest import PDF, Team, make_message
from tests.integration.web.conftest import HX, make_enquiry
from tests.support.fake_llm import FakeLLMClient

JSON = {"Accept": "application/json"}
GOOD = {
    "subject": "AI workshops for Acme Logistics",
    "body": "Hello Priya,\n\nI saw Acme Logistics wants a GenAI workshop. We can help.\n\nBest regards,\nAnn Rao",
}
BRIEF_ANSWER = {
    "action": "finish",
    "thought": "no website",
    "brief": {
        "company_name": "Acme Logistics", "what_they_do": "Runs 14 warehouses across India.", "size_hints": "unknown",
        "ai_training_needs": ["Planner upskilling"], "suggested_offering": "AI corporate workshops",
        "personalisation_hooks": ["Runs 14 warehouses across India"], "sources": [],
    },
}


@pytest.fixture
def web(team: Team) -> Iterator[TestClient]:
    set_llm_client(FakeLLMClient(responder=lambda req: GOOD))
    with TestClient(create_app()) as c:
        yield c


def act_as(web: TestClient, member) -> None:  # noqa: ANN001
    assert web.post("/team/acting", data={"member_id": member.id, "next": "/outreach"}, follow_redirects=False).status_code == 303


def draft_via_web(web: TestClient, **over: str) -> int:
    data = {"to": "priya@acme.example", "contact_name": "Priya Menon", "company": "Acme Logistics",
            "requirement": "GenAI workshop for 40 managers", "offering_id": "", "template_id": "", "deck_id": "none"}  # fmt: skip
    data.update(over)
    resp = web.post("/outreach/draft", data=data, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return int(resp.headers["location"].rsplit("/", 1)[1])


def page(web: TestClient, msg_id: int) -> str:
    resp = web.get(f"/outreach/messages/{msg_id}")
    assert resp.status_code == 200, resp.text[:500]
    return resp.text


# ------------------------------------------------------------------------ compose -> draft -> approve -> send
def test_full_flow_draft_approve_send_once(web: TestClient, team: Team) -> None:
    act_as(web, team.ann)
    msg_id = draft_via_web(web)
    html = page(web, msg_id)
    assert "Sending as <strong>ann@team.example</strong>" in html
    assert "AI workshops for Acme Logistics" in html and 'id="approve-btn"' in html
    assert 'id="send-btn"' not in html  # not approved yet: no Send control at all

    assert web.post(f"/outreach/messages/{msg_id}/approve", follow_redirects=False).status_code == 303
    html = page(web, msg_id)
    with get_sessionmaker()() as db:
        msg = db.get(OutreachMessage, msg_id)
        assert msg is not None and msg.state == "approved" and msg.content_sha256
        sha = msg.content_sha256
    assert 'id="send-btn"' in html and sha[:12] in html and "Send now from ann@team.example" in html

    resp = web.post(f"/outreach/messages/{msg_id}/send", follow_redirects=False)
    assert resp.status_code == 303
    gmail = team.gmail[team.ann.id]
    assert len(gmail.sent) == 1
    html = page(web, msg_id)
    assert 'id="send-btn"' not in html and "cannot be sent again" in html and "msg-1" in html
    # a second click / replay does nothing
    again = web.post(f"/outreach/messages/{msg_id}/send", follow_redirects=True)
    assert "already sent" in again.text and len(gmail.sent) == 1
    with get_sessionmaker()() as db:
        kinds = [e.kind for e in db.execute(select(OutreachEvent).where(OutreachEvent.message_id == msg_id).order_by(OutreachEvent.id)).scalars()]
        msg = db.get(OutreachMessage, msg_id)
        assert msg is not None and msg.state == "sent" and msg.gmail_message_id == "msg-1" and msg.gmail_thread_id == "thr-1"
    assert kinds == ["drafted", "approved", "sending", "sent"]
    assert "Timeline" in html and "Sent" in html
    outbox = web.get("/outreach").text
    assert "1 of 40" in outbox and 'data-state="sent"' in outbox


def test_unapproved_send_is_refused_with_not_approved(web: TestClient, team: Team) -> None:
    act_as(web, team.ann)
    msg_id = draft_via_web(web)
    resp = web.post(f"/outreach/messages/{msg_id}/send", headers=JSON)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "not_approved"
    browser = web.post(f"/outreach/messages/{msg_id}/send", follow_redirects=True)  # plain form post: red flash, no error page
    assert "Only an approved message can be sent" in browser.text and 'id="outreach-flash"' in browser.text
    assert team.gmail[team.ann.id].sent == [] and team.gmail[team.ann.id].calls == []


def test_editing_an_approved_message_resets_approval_and_hides_send(web: TestClient, team: Team) -> None:
    act_as(web, team.ann)
    msg_id = draft_via_web(web)
    web.post(f"/outreach/messages/{msg_id}/approve")
    assert 'id="send-btn"' in page(web, msg_id)
    assert "resets its approval" in page(web, msg_id)  # the notice is shown before editing
    resp = web.post(
        f"/outreach/messages/{msg_id}/edit",
        data={"to": "priya@acme.example", "cc": "", "subject": "New subject", "body": GOOD["body"], "deck_id": "none"},
        follow_redirects=True,
    )
    assert "approval was reset" in resp.text
    html = page(web, msg_id)
    assert 'id="send-btn"' not in html and 'id="approve-btn"' in html and "New subject" in html
    assert web.post(f"/outreach/messages/{msg_id}/send", headers=JSON).json()["error"]["code"] == "not_approved"


def test_other_members_cannot_act_and_non_admins_cannot_read(web: TestClient, team: Team) -> None:
    act_as(web, team.ann)
    msg_id = draft_via_web(web)
    web.post(f"/outreach/messages/{msg_id}/approve")
    act_as(web, team.bob)
    for action in ("send", "approve", "reject"):
        resp = web.post(f"/outreach/messages/{msg_id}/{action}", headers=JSON)
        assert resp.status_code == 403 and resp.json()["error"]["code"] == "not_owner", action
    assert web.get(f"/outreach/messages/{msg_id}").status_code == 404
    assert f'id="message-{msg_id}"' not in web.get("/outreach?scope=all").text  # scope=all is admin-only
    act_as(web, team.admin)
    html = page(web, msg_id)  # admin can read it ...
    assert "belongs to Ann Rao" in html and 'id="send-btn"' not in html and 'id="approve-btn"' not in html  # ... but not act
    assert web.post(f"/outreach/messages/{msg_id}/send", headers=JSON).json()["error"]["code"] == "not_owner"
    assert team.gmail[team.ann.id].sent == []


def test_gmail_not_connected_blocks_send_with_a_link_to_team(web: TestClient, team: Team, db: Session) -> None:
    act_as(web, team.ann)
    msg_id = draft_via_web(web)
    web.post(f"/outreach/messages/{msg_id}/approve")
    connections.disconnect(db, team.ann.id)
    db.commit()
    html = page(web, msg_id)
    assert 'id="gmail-block"' in html and 'href="/team"' in html and 'id="send-btn"' not in html
    assert web.post(f"/outreach/messages/{msg_id}/send", headers=JSON).json()["error"]["code"] == "gmail_not_connected"


def test_daily_cap_hides_send_and_outbox_shows_usage(web: TestClient, team: Team, monkeypatch: pytest.MonkeyPatch) -> None:
    act_as(web, team.ann)
    msg_id = draft_via_web(web)
    web.post(f"/outreach/messages/{msg_id}/approve")
    monkeypatch.setenv("SALES0_GMAIL_DAILY_SEND_CAP", "1")
    reset_settings()
    assert 'id="send-btn"' in page(web, msg_id)
    web.post(f"/outreach/messages/{msg_id}/send")
    second = draft_via_web(web, company="Beta Corp")
    web.post(f"/outreach/messages/{second}/approve")
    html = page(web, second)
    assert 'id="send-btn"' not in html and "limit of 1 emails is reached" in html
    assert "<strong>1 of 1</strong>" in web.get("/outreach").text
    assert web.post(f"/outreach/messages/{second}/send", headers=JSON).json()["error"]["code"] == "daily_cap_reached"


def test_unknown_outcome_failure_needs_the_explicit_confirmation(web: TestClient, team: Team, db: Session) -> None:
    act_as(web, team.ann)
    msg_id = draft_via_web(web)
    web.post(f"/outreach/messages/{msg_id}/approve")
    gmail = team.gmail[team.ann.id]
    real_send = gmail.send_draft
    gmail.send_draft = lambda approved: (_ for _ in ()).throw(GmailTransientError("network down"))  # type: ignore[method-assign]
    web.post(f"/outreach/messages/{msg_id}/send")
    html = page(web, msg_id)
    assert "not known whether this email went out" in html
    assert 'id="send-btn"' not in html and 'id="retry-btn"' not in html
    assert 'id="confirm-not-sent"' in html and "it was NOT sent" in html
    assert web.post(f"/outreach/messages/{msg_id}/send", headers=JSON).json()["error"]["code"] == "outcome_unknown"
    assert web.post(f"/outreach/messages/{msg_id}/retry", headers=JSON).json()["error"]["code"] == "confirmation_required"
    gmail.send_draft = real_send  # type: ignore[method-assign]
    web.post(f"/outreach/messages/{msg_id}/retry", data={"confirm_not_sent": "1"})
    assert len(gmail.sent) == 1 and 'id="retry-unknown-form"' not in page(web, msg_id)


def test_known_failure_shows_retry_not_send(web: TestClient, team: Team, db: Session) -> None:
    msg = make_message(db, team.ann, state="failed")
    msg.content_sha256, msg.error = "a" * 64, "Gmail refused the send: nope"
    db.commit()
    act_as(web, team.ann)
    html = page(web, msg.id)
    assert "Nothing was sent." in html and 'id="send-btn"' not in html


def test_placeholders_are_flagged_and_approve_is_refused(web: TestClient, team: Team, db: Session) -> None:
    msg = make_message(db, team.ann, body="Hello {first_name},\n\n[Your Name]")
    act_as(web, team.ann)
    html = page(web, msg.id)
    assert 'id="placeholder-warning"' in html and "first_name" in html
    assert web.post(f"/outreach/messages/{msg.id}/approve", headers=JSON).json()["error"]["code"] == "unfilled_placeholders"


def test_reject_and_unknown_message(web: TestClient, team: Team) -> None:
    act_as(web, team.ann)
    msg_id = draft_via_web(web)
    web.post(f"/outreach/messages/{msg_id}/reject", data={"reason": "wrong person"})
    html = page(web, msg_id)
    assert "will not be sent" in html and 'id="send-btn"' not in html and 'id="approve-btn"' not in html
    assert web.get("/outreach/messages/99999").status_code == 404


# ------------------------------------------------------------------------ outbox
def test_outbox_filters_scope_and_stuck_warning(web: TestClient, team: Team, db: Session) -> None:
    a = make_message(db, team.ann, subject="Ann draft")
    stuck = make_message(db, team.ann, subject="Ann stuck", state="sending")
    stuck.updated_at = utcnow() - timedelta(minutes=30)
    make_message(db, team.bob, subject="Bob draft")
    db.commit()
    act_as(web, team.ann)
    html = web.get("/outreach").text
    assert "Ann draft" in html and "Bob draft" not in html and 'id="stuck-warning"' in html
    assert "Ann draft" not in web.get("/outreach?state=sending").text and "Ann stuck" in web.get("/outreach?state=sending").text
    assert 'id="filter-scope"' not in html  # only admins get the scope filter
    act_as(web, team.admin)
    allhtml = web.get("/outreach?scope=all").text
    assert "Ann draft" in allhtml and "Bob draft" in allhtml and "Ann Rao" in allhtml
    assert "Bob draft" not in web.get("/outreach").text
    assert a.id


def test_outbox_empty_state(web: TestClient, team: Team) -> None:
    act_as(web, team.ann)
    assert 'id="outbox-empty"' in web.get("/outreach").text


# ------------------------------------------------------------------------ compose / research
def test_compose_prefills_from_an_enquiry(web: TestClient, team: Team) -> None:
    eid = make_enquiry("P1", company="Acme Logistics", contact_person="Priya Menon", email="priya@acme.example", requirement_summary="GenAI workshop for 40 managers")
    html = web.get(f"/outreach/compose?enquiry_id={eid}").text
    assert 'value="Acme Logistics"' in html and 'value="priya@acme.example"' in html and "GenAI workshop for 40 managers" in html
    assert f"Prefilled from enquiry #{eid}" in html
    assert web.get("/outreach/compose?enquiry_id=99999").status_code == 404


def test_offering_change_returns_that_offerings_template_and_deck(web: TestClient, team: Team, db: Session, offering, deck) -> None:
    from sales0.services.outreach import decks, templates

    templates.create_template(db, name="Workshop intro", subject="Hi {first_name}", body="Hello {first_name}", offering_id=offering.id)
    decks.set_deck_offering(db, deck.id, offering.id)
    db.commit()
    html = web.get(f"/outreach/compose/defaults?offering_id={offering.id}").text
    assert re.search(r'<option value="\d+" selected>Workshop intro', html) and re.search(rf'<option value="{deck.id}" selected>', html)


def test_research_panel_then_draft_uses_the_brief(web: TestClient, team: Team) -> None:
    fake = FakeLLMClient(responder=lambda req: BRIEF_ANSWER if req.feature == "research" else GOOD)
    set_llm_client(fake)
    act_as(web, team.ann)
    resp = web.post("/outreach/research", data={"company": "Acme Logistics", "website": "", "requirement": "workshop"}, headers=HX)
    assert resp.status_code == 200
    assert 'name="brief_id"' in resp.text and "Runs 14 warehouses" in resp.text and "Personalisation hooks" in resp.text
    assert "toast--ok" in resp.text and 'id="cost-badge"' in resp.text  # header cost refreshed
    brief_id = int(re.search(r'name="brief_id" value="(\d+)"', resp.text).group(1))  # type: ignore[union-attr]
    with session_scope() as db:
        assert db.get(CompanyBrief, brief_id) is not None
    msg_id = draft_via_web(web, brief_id=str(brief_id))
    assert "Runs 14 warehouses" in page(web, msg_id) and 'id="message-hooks"' in page(web, msg_id)
    assert any("Runs 14 warehouses" in str(r.messages) for r in fake.requests if r.feature == "drafting")


def test_research_and_draft_errors_are_toasts_and_keep_the_form(web: TestClient, team: Team) -> None:
    for path in ("/outreach/research", "/outreach/draft"):
        resp = web.post(path, data={"company": "  "}, headers=HX)
        assert resp.status_code == 200 and "toast--error" in resp.text and resp.headers["HX-Retarget"] == "#toasts"
        assert "company" in resp.text.lower()
    bad = web.post("/outreach/research", data={"company": "Acme", "website": "ftp://x"}, headers=HX)
    assert "toast--error" in bad.text and "website" in bad.text.lower()


def test_htmx_draft_redirects_with_hx_redirect(web: TestClient, team: Team) -> None:
    act_as(web, team.ann)
    resp = web.post("/outreach/draft", data={"company": "Acme Logistics", "to": "p@acme.example", "deck_id": "none"}, headers=HX)
    assert resp.status_code == 204 and re.fullmatch(r"/outreach/messages/\d+", resp.headers["HX-Redirect"])


def test_draft_failure_from_the_ai_is_a_clear_error(web: TestClient, team: Team) -> None:
    from sales0.llm import LLMTransientError

    set_llm_client(FakeLLMClient([LLMTransientError("overloaded")]))
    resp = web.post("/outreach/draft", data={"company": "Acme", "to": "p@acme.example"}, headers=HX)
    assert "toast--error" in resp.text and "AI service is unavailable" in resp.text


# ------------------------------------------------------------------------ decks
def upload(web: TestClient, data: bytes, filename: str = "Deck.pdf", **form: str):  # noqa: ANN201
    return web.post("/outreach/decks", files={"file": (filename, data, "application/pdf")}, data=form, follow_redirects=True)


def test_deck_upload_map_download_delete(web: TestClient, team: Team, offering) -> None:
    resp = upload(web, PDF, "Workshops Deck.pdf", offering_id=str(offering.id), name="Workshops")
    assert "Deck &#39;Workshops&#39; uploaded" in resp.text or "Deck 'Workshops' uploaded" in resp.text
    assert 'id="deck-table"' in resp.text
    deck_id = int(re.search(r'id="deck-(\d+)"', resp.text).group(1))  # type: ignore[union-attr]
    dl = web.get(f"/outreach/decks/{deck_id}/file")
    assert dl.status_code == 200 and dl.content == PDF and dl.headers["content-type"] == "application/pdf"
    again = upload(web, PDF, "copy.pdf")
    assert len(re.findall(r'id="deck-\d+"', again.text)) == 1  # same bytes: no duplicate
    other = web.post(f"/outreach/decks/{deck_id}/offering", data={"offering_id": ""}, follow_redirects=True)
    assert "updated" in other.text
    gone = web.post(f"/outreach/decks/{deck_id}/delete", follow_redirects=True)
    assert 'id="decks-empty"' in gone.text and "can still be sent" in gone.text


def test_deck_upload_rejections_are_clear_messages(web: TestClient, team: Team, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "Only PDF files" in upload(web, b"GIF89a not a pdf", "x.pdf").text
    assert "That file is empty" in upload(web, b"", "x.pdf").text
    monkeypatch.setenv("SALES0_DECK_MAX_BYTES", "100")
    reset_settings()
    big = upload(web, PDF + b"x" * 500, "big.pdf")
    assert "the limit is" in big.text and 'id="deck-table"' not in big.text
    api = web.post("/outreach/decks", files={"file": ("big.pdf", PDF + b"x" * 500, "application/pdf")}, headers=JSON)
    assert api.status_code == 413 and api.json()["error"]["code"] == "deck_too_large"


def test_attaching_a_deck_changes_the_approval_hash(web: TestClient, team: Team, deck) -> None:
    act_as(web, team.ann)
    msg_id = draft_via_web(web)
    first = re.search(r"\(hash ([0-9a-f]{12})\)", page(web, msg_id)).group(1)  # type: ignore[union-attr]
    web.post(
        f"/outreach/messages/{msg_id}/edit",
        data={"to": "priya@acme.example", "cc": "", "subject": GOOD["subject"], "body": GOOD["body"], "deck_id": str(deck.id)},
    )
    html = page(web, msg_id)
    second = re.search(r"\(hash ([0-9a-f]{12})\)", html).group(1)  # type: ignore[union-attr]
    assert first != second and "Workshops Deck.pdf" in html
    web.post(f"/outreach/messages/{msg_id}/approve")
    web.post(f"/outreach/messages/{msg_id}/send")
    sent = team.gmail[team.ann.id].sent
    assert len(sent) == 1 and b"application/pdf" in sent[0]["raw"] and b"Workshops Deck.pdf" in sent[0]["raw"]
    assert sent[0]["approved"].sha256[:12] == second


# ------------------------------------------------------------------------ templates
def test_template_crud_preview_and_validation(web: TestClient, team: Team, offering) -> None:
    form = {"template_id": "", "name": "Intro", "subject": "Hi {first_name}", "body": "Hello {first_name} at {company}", "offering_id": str(offering.id)}
    created = web.post("/outreach/templates/save", data=form, follow_redirects=True)
    assert "created" in created.text and 'id="template-table"' in created.text
    tid = int(re.search(r'id="template-(\d+)"', created.text).group(1))  # type: ignore[union-attr]
    edit = web.get(f"/outreach/templates?edit={tid}").text
    assert "Edit template" in edit and "Hello {first_name} at {company}" in edit
    saved = web.post("/outreach/templates/save", data={**form, "template_id": str(tid), "name": "Intro v2"}, follow_redirects=True)
    assert "Intro v2" in saved.text
    bad = web.post("/outreach/templates/save", data={**form, "body": "Hi {nonsense}"}, follow_redirects=True)
    assert "Unknown placeholder" in bad.text and 'id="outreach-flash"' in bad.text
    preview = web.post("/outreach/templates/preview", data={"subject": "Hi {first_name}", "body": "Dear {first_name} of {company} {oops}", "offering_id": ""}, headers=HX)
    assert "Dear Priya of Acme Logistics {oops}" in preview.text and "Unknown placeholder(s): oops" in preview.text
    gone = web.post(f"/outreach/templates/{tid}/delete", follow_redirects=True)
    assert 'id="templates-empty"' in gone.text and "deleted" in gone.text
    assert web.get("/outreach/templates?edit=99999").status_code == 404
