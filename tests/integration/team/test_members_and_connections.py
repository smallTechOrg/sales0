"""Member seeding and per-member Gmail connection storage/refusal/disconnect."""

from __future__ import annotations

import sqlite3

import httpx
import pytest
from sqlalchemy import select

from sales0.config import get_settings, reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import GmailConnection
from sales0.errors import DomainError
from sales0.integrations.gmail.auth import SCOPE_COMPOSE, SCOPES, StoredToken
from sales0.integrations.gmail.client import FakeGmailClient
from sales0.integrations.gmail.errors import GmailAuthError, GmailNotConfigured, GmailScopeError
from sales0.services.team import connections, members

SECRET = "1//plaintext-refresh-token-ZZZ"


@pytest.fixture(autouse=True)
def _env(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SECRET_KEY", "test-secret-key-0123456789abcdef")
    monkeypatch.setenv("SALES0_ADMIN_EMAIL", " Boss@Team.Example ")
    reset_settings()


def _token(email: str | None = "ann@team.example", scopes: list[str] | None = None) -> StoredToken:
    return StoredToken("cid", "csecret", SECRET, list(scopes or SCOPES), email)


def _ann() -> int:
    with session_scope() as db:
        return members.add_member(db, "Ann", "Ann@Team.Example").id


def test_seed_admin_is_idempotent_and_lowercases() -> None:
    for _ in range(3):
        with session_scope() as db:
            members.seed_admin(db, get_settings())
    with session_scope() as db:
        rows = members.list_members(db)
    assert [(m.email, m.role, m.active, m.name) for m in rows] == [("boss@team.example", "admin", True, "Admin")]


def test_add_member_validates_and_rejects_duplicates() -> None:
    _ann()
    with session_scope() as db:
        with pytest.raises(DomainError) as dup:
            members.add_member(db, "Other", "ann@team.example")
        assert dup.value.code == "member_exists"
        with pytest.raises(DomainError):
            members.add_member(db, "Bad", "not-an-email")
        with pytest.raises(DomainError):
            members.add_member(db, " ", "x@y.example")


def test_cannot_deactivate_the_only_admin() -> None:
    with session_scope() as db:
        admin = members.seed_admin(db)
        with pytest.raises(DomainError) as exc:
            members.set_active(db, admin.id, False)
        assert exc.value.code == "last_admin"


def test_token_is_encrypted_at_rest_and_roundtrips() -> None:
    mid = _ann()
    with session_scope() as db:
        view = connections.store_token(db, mid, _token())
    assert view.status == "connected" and view.email == "ann@team.example"
    path = get_settings().resolved_database_url.removeprefix("sqlite:///")
    raw = b"".join(line.encode() for line in sqlite3.connect(path).iterdump())
    assert SECRET.encode() not in raw  # never in the DB in plaintext
    with session_scope() as db:
        row = db.execute(select(GmailConnection)).scalar_one()
        assert SECRET not in (row.token_encrypted or "") and row.scopes == sorted(SCOPES)
        assert connections.load_token(db, mid).refresh_token == SECRET


def test_email_mismatch_is_refused_and_nothing_is_stored() -> None:
    mid = _ann()
    with session_scope() as db:
        with pytest.raises(DomainError) as exc:
            connections.store_token(db, mid, _token("someone.else@gmail.example"))
        assert exc.value.code == "gmail_email_mismatch" and "ann@team.example" in exc.value.message
    with session_scope() as db:
        row = db.execute(select(GmailConnection)).scalar_one()
        assert row.token_encrypted is None and row.status == "disconnected" and "someone.else" in (row.last_error or "")
        with pytest.raises(GmailNotConfigured):
            connections.load_token(db, mid)


def test_missing_email_and_missing_refresh_token_are_refused() -> None:
    mid = _ann()
    with session_scope() as db:
        with pytest.raises(DomainError) as exc:
            connections.store_token(db, mid, _token(None))
        assert exc.value.code == "gmail_email_unknown"


def test_insufficient_scopes_store_with_scope_insufficient_status() -> None:
    mid = _ann()
    with session_scope() as db:
        view = connections.store_token(db, mid, _token(scopes=[SCOPE_COMPOSE, "openid"]))
    assert view.status == "scope_insufficient" and view.last_error


def test_status_not_connected_without_a_row() -> None:
    mid = _ann()
    with session_scope() as db:
        assert connections.connection_status(db, mid).status == "not_connected"


def test_record_failure_flips_status() -> None:
    mid = _ann()
    with session_scope() as db:
        connections.store_token(db, mid, _token())
    connections.record_failure(mid, GmailAuthError("revoked"))
    with session_scope() as db:
        assert connections.connection_status(db, mid).status == "needs_reauth"
    connections.record_failure(mid, GmailScopeError("lacks scope"))
    with session_scope() as db:
        assert connections.connection_status(db, mid).status == "scope_insufficient"
    connections.record_failure(mid, RuntimeError("other"))  # ignored
    with session_scope() as db:
        assert connections.connection_status(db, mid).status == "scope_insufficient"


def test_client_for_member_real_path_and_refusals() -> None:
    mid = _ann()
    with pytest.raises(GmailNotConfigured):
        connections.client_for_member(mid)
    with session_scope() as db:
        connections.store_token(db, mid, _token())
    client = connections.client_for_member(mid, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    assert type(client).__name__ == "RealGmailClient"
    connections.record_failure(mid, GmailAuthError("x"))
    with pytest.raises(GmailAuthError):
        connections.client_for_member(mid)


def test_client_factory_seam_and_identity_check() -> None:
    mid = _ann()
    connections.set_client_factory(lambda member_id, http: FakeGmailClient("ann@team.example"))
    client = connections.client_for_member(mid)
    assert connections.verify_client_identity(mid, client) == "ann@team.example"
    with session_scope() as db:
        connections.store_token(db, mid, _token())
    wrong = FakeGmailClient("mallory@team.example")
    with pytest.raises(GmailAuthError):
        connections.verify_client_identity(mid, wrong)
    with session_scope() as db:
        assert connections.connection_status(db, mid).status == "needs_reauth"


def test_disconnect_revokes_via_google_then_wipes_the_token() -> None:
    mid = _ann()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    with session_scope() as db:
        connections.store_token(db, mid, _token())
    with session_scope() as db:
        view = connections.disconnect(db, mid, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert view.status == "disconnected" and view.last_error is None
    assert len(seen) == 1 and str(seen[0].url) == "https://oauth2.googleapis.com/revoke"
    assert f"token={SECRET}".replace("/", "%2F") in seen[0].content.decode()
    with session_scope() as db:
        row = db.execute(select(GmailConnection)).scalar_one()
        assert row.token_encrypted is None and row.scopes is None


def test_disconnect_still_wipes_when_google_is_unreachable() -> None:
    mid = _ann()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with session_scope() as db:
        connections.store_token(db, mid, _token())
    with session_scope() as db:
        view = connections.disconnect(db, mid, http=httpx.Client(transport=httpx.MockTransport(boom)))
    assert view.status == "disconnected" and "revoke" in (view.last_error or "")
    with session_scope() as db:
        assert db.execute(select(GmailConnection)).scalar_one().token_encrypted is None


def test_changed_secret_key_makes_the_token_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    mid = _ann()
    with session_scope() as db:
        connections.store_token(db, mid, _token())
    monkeypatch.setenv("SALES0_SECRET_KEY", "a-completely-different-key-987654321")
    reset_settings()
    with session_scope() as db:
        with pytest.raises(GmailAuthError):
            connections.load_token(db, mid)
