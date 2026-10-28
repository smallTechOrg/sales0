"""The full connect -> Google -> callback journey over a TestClient, with Google faked."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.clock import utcnow
from sales0.config import get_settings
from sales0.db.engine import session_scope
from sales0.db.models import GmailConnection, TeamMember
from sales0.integrations.gmail.auth import SCOPES
from sales0.services.team import oauth
from tests.integration.team.conftest import ADMIN_EMAIL, REFRESH, FakeGoogle, start_connect


def _callback(client: TestClient, **params: str) -> str:
    resp = client.get("/oauth/google/callback", params=params, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/team"
    return client.get("/team").text


def _status() -> list[str]:
    with session_scope() as db:
        return [c.status for c in db.execute(select(GmailConnection)).scalars()]


def test_connect_builds_a_correct_google_url(client: TestClient) -> None:
    q = start_connect(client)
    assert q["client_id"] == "cid.apps.example"
    assert q["redirect_uri"] == "http://127.0.0.1:8766/oauth/google/callback"
    assert q["response_type"] == "code" and q["access_type"] == "offline" and q["prompt"] == "consent"
    assert q["login_hint"] == ADMIN_EMAIL and q["scope"].split() == list(SCOPES)
    assert q["code_challenge_method"] == "S256" and len(q["state"]) >= 32
    assert "secret" not in str(q).lower().replace("client_secret_path", "")


def test_full_flow_stores_an_encrypted_connection(client: TestClient, google: FakeGoogle) -> None:
    q = start_connect(client)
    page = _callback(client, state=q["state"], code="auth-code-1")
    assert f"Gmail connected as {ADMIN_EMAIL}" in page
    assert "conn--connected" in page and "Disconnect" in page and "Reconnect my Gmail" in page
    # the code exchange carried the PKCE verifier matching the challenge, and the redirect uri
    exchange = next(c for c in google.calls if c[1].endswith("/token"))[2]
    assert exchange["code"] == "auth-code-1" and exchange["redirect_uri"] == q["redirect_uri"]
    assert oauth.code_challenge(exchange["code_verifier"]) == q["code_challenge"]
    assert _status() == ["connected"]
    path = get_settings().resolved_database_url.removeprefix("sqlite:///")
    assert REFRESH.encode() not in b"".join(line.encode() for line in sqlite3.connect(path).iterdump())
    # the flash is one-shot
    assert "Gmail connected as" not in client.get("/team").text


def test_state_cannot_be_reused(client: TestClient) -> None:
    q = start_connect(client)
    _callback(client, state=q["state"], code="c1")
    page = _callback(client, state=q["state"], code="c2")
    assert "unknown or was already used" in page


def test_unknown_missing_and_expired_state_are_rejected(client: TestClient, google: FakeGoogle) -> None:
    assert "unknown or was already used" in _callback(client, state="forged", code="c")
    assert "unknown or was already used" in _callback(client, code="c")
    with session_scope() as db:
        admin = db.execute(select(TeamMember)).scalars().first()
        assert admin is not None
        expired = oauth.STORE.create(admin.id, "http://127.0.0.1:8766/oauth/google/callback", now=utcnow() - timedelta(hours=1))
    assert "took too long" in _callback(client, state=expired.state, code="c")
    assert _status() == [] and not any(c[1].endswith("/token") for c in google.calls)


def test_state_started_by_another_member_is_rejected(client: TestClient, google: FakeGoogle) -> None:
    q = start_connect(client)  # started as the admin
    client.post("/team/members", data={"name": "Bob", "email": "bob@team.example", "role": "member"})
    with session_scope() as db:
        bob = db.execute(select(TeamMember).where(TeamMember.email == "bob@team.example")).scalar_one()
    assert client.post("/team/acting", data={"member_id": bob.id}, follow_redirects=False).status_code == 303
    page = _callback(client, state=q["state"], code="c")
    assert "different team member" in page
    assert _status() == [] and not any(c[1].endswith("/token") for c in google.calls)


def test_wrong_google_account_is_refused_with_a_clear_message(client: TestClient, google: FakeGoogle) -> None:
    google.email = "someone.else@gmail.example"
    q = start_connect(client)
    page = _callback(client, state=q["state"], code="c")
    assert "someone.else@gmail.example" in page and ADMIN_EMAIL in page and "Nothing was connected" in page
    assert _status() == ["disconnected"]
    with session_scope() as db:
        assert db.execute(select(GmailConnection)).scalar_one().token_encrypted is None


def test_access_denied_is_handled_cleanly(client: TestClient, google: FakeGoogle) -> None:
    q = start_connect(client)
    page = _callback(client, state=q["state"], error="access_denied")
    assert "cancelled" in page and _status() == []
    assert not any(c[1].endswith("/token") for c in google.calls)


def test_missing_refresh_token_and_rejected_code_are_reported(client: TestClient, google: FakeGoogle) -> None:
    google.refresh_token = None
    page = _callback(client, state=start_connect(client)["state"], code="c")
    assert "no refresh token" in page
    google.refresh_token, google.token_status = REFRESH, 400
    page = _callback(client, state=start_connect(client)["state"], code="c")
    assert "Google rejected the sign-in" in page and _status() == []


def test_partial_scope_grant_is_flagged(client: TestClient, google: FakeGoogle) -> None:
    google.scope = "openid https://www.googleapis.com/auth/userinfo.email"
    page = _callback(client, state=start_connect(client)["state"], code="c")
    assert "permission" in page and _status() == ["scope_insufficient"]
    assert "conn--scope_insufficient" in page


def test_disconnect_revokes_and_marks_disconnected(client: TestClient, google: FakeGoogle) -> None:
    _callback(client, state=start_connect(client)["state"], code="c")
    with session_scope() as db:
        admin = db.execute(select(TeamMember)).scalars().first()
        assert admin is not None
    resp = client.post("/team/gmail/disconnect", data={"member_id": admin.id}, follow_redirects=True)
    assert "Gmail disconnected for Ada Admin" in resp.text and "conn--disconnected" in resp.text
    assert any(c[1].endswith("/revoke") for c in google.calls)
    assert _status() == ["disconnected"]


@pytest.fixture
def public_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server is configured to answer to this name, so the Host guard lets the request reach the OAuth service."""
    monkeypatch.setenv("SALES0_HOST", "sales.example.com")


def test_connect_refuses_a_non_loopback_host(public_host: None, client: TestClient) -> None:
    resp = client.post("/team/gmail/connect", headers={"host": "sales.example.com"}, follow_redirects=True)
    assert "only works when sales0 is opened on localhost or 127.0.0.1" in resp.text


def test_connect_without_client_secret_explains_what_to_do(client: TestClient, client_secret) -> None:  # type: ignore[no-untyped-def]
    client_secret.unlink()
    resp = client.post("/team/gmail/connect", follow_redirects=True)
    assert "OAuth client secret not found" in resp.text


def test_callback_query_is_accepted_cross_site(client: TestClient) -> None:
    """Google redirects with a GET carrying a foreign Referer; the CSRF origin check must not block it."""
    q = start_connect(client)
    resp = client.get(
        "/oauth/google/callback", params={"state": q["state"], "code": "c"},
        headers={"referer": "https://accounts.google.com/", "origin": "https://accounts.google.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
