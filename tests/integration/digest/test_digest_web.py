"""Digest pages through the real app (TestClient, FakeLLMClient, FakeGmailClient)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from sales0.clock import utcnow
from sales0.llm import set_llm_client
from sales0.services import digest as dg
from sales0.web.app import create_app
from tests.integration.digest.conftest import NOW, Team, add_enquiry
from tests.support.fake_llm import FakeLLMClient

JSON = {"Accept": "application/json"}
SCRIPT = "<script>alert(1)</script>"


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    fake = FakeLLMClient(responder=lambda req: {"lines": ["Quiet day."]})
    set_llm_client(fake)
    return fake


@pytest.fixture
def web(team: Team, fake_llm: FakeLLMClient) -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        assert c.post("/team/acting", data={"member_id": team.ann.id, "next": "/digest"}, follow_redirects=False).status_code == 303
        yield c


def test_empty_state_offers_build_now(web: TestClient) -> None:
    resp = web.get("/digest")
    assert resp.status_code == 200
    assert "No digest has been built yet" in resp.text and "Build now" in resp.text
    assert 'href="/digest"' in resp.text  # nav entry


def test_build_now_then_latest_shows_sections_summary_and_history(web: TestClient, db: Session, fake_llm: FakeLLMClient) -> None:
    add_enquiry(db, added=utcnow() - timedelta(hours=1), company="Acme Logistics")
    resp = web.post("/digest/build", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/digest/")
    page = web.get(resp.headers["location"])
    assert "built." in page.text  # flash
    for text in ("New relevant enquiries", "Awaiting review", "Outreach", "Discovery health", "AI cost", "Acme Logistics", "Quiet day."):
        assert text in page.text
    assert 'id="copy-digest"' in page.text and "Create Gmail draft to myself" in page.text
    assert fake_llm.calls == 1
    latest = web.get("/digest")
    assert latest.status_code == 200 and 'id="digest-history"' in latest.text and "Acme Logistics" in latest.text
    assert "Rebuild now" in latest.text
    web.post("/digest/build")  # same day: still one history row
    assert web.get("/digest").text.count('href="/digest/') == 1


def test_digest_content_is_escaped(web: TestClient, db: Session) -> None:
    add_enquiry(db, added=utcnow() - timedelta(minutes=5), company=SCRIPT, summary=SCRIPT)  # the page uses the real clock
    web.post("/digest/build")
    html = web.get("/digest").text
    assert SCRIPT not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_unknown_digest_id_is_404(web: TestClient) -> None:
    assert web.get("/digest/999", headers=JSON).status_code == 404


def test_gmail_draft_button_creates_only_a_draft(web: TestClient, db: Session, team: Team) -> None:
    view = dg.generate_digest(db, now=NOW, with_summary=False)
    resp = web.post(f"/digest/{view.id}/gmail-draft", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == f"/digest/{view.id}"
    client = team.gmail[team.ann.id]
    assert [c[0] for c in client.calls if c[0] in ("create_draft", "send_draft")] == ["create_draft"]
    assert client.sent == []
    page = web.get(f"/digest/{view.id}")
    assert "A Gmail draft was created" in page.text and "nothing was sent" in page.text and 'id="draft-status"' in page.text


def test_gmail_draft_errors_are_flashed_not_raised(web: TestClient, db: Session, team: Team) -> None:
    from sales0.integrations.gmail.client import FakeGmailClient

    view = dg.generate_digest(db, now=NOW, with_summary=False)
    team.gmail[team.ann.id] = FakeGmailClient(email="other@example.com")
    resp = web.post(f"/digest/{view.id}/gmail-draft", follow_redirects=False)
    assert resp.status_code == 303
    page = web.get(f"/digest/{view.id}")
    assert "flash--error" in page.text and "No draft was created" in page.text
    json_resp = web.post(f"/digest/{view.id}/gmail-draft", headers=JSON)
    assert json_resp.status_code == 409 and json_resp.json()["error"]["code"] == "sender_mismatch"
