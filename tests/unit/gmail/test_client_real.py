"""RealGmailClient against a fake Google (httpx.MockTransport). No network."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import httpx
import pytest

from sales0.integrations.gmail.auth import SCOPE_COMPOSE, SCOPES, StoredToken, TokenProvider
from sales0.integrations.gmail.client import ApprovedMessage, RealGmailClient
from sales0.integrations.gmail.errors import (
    ApprovalMismatch,
    GmailApiError,
    GmailAuthError,
    GmailNotFound,
    GmailScopeError,
    GmailTransientError,
)
from sales0.integrations.gmail.mime import build_message, normalise


class FakeGoogle:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.drafts: dict[str, bytes] = {}
        self.script: list[httpx.Response] = []  # consumed first for API (non-token) calls
        self.token_response = httpx.Response(200, json={"access_token": "AT1", "expires_in": 3600})
        self.token_calls = 0
        self.sent: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.host == "oauth2.googleapis.com":
            self.token_calls += 1
            return self.token_response
        if self.script:
            return self.script.pop(0)
        path = req.url.path
        if req.method == "POST" and path.endswith("/drafts") and "upload" in path:
            did = f"d{len(self.drafts) + 1}"
            self.drafts[did] = req.content
            return httpx.Response(200, json={"id": did})
        if req.method == "POST" and path.endswith("/drafts/send"):
            did = json.loads(req.content)["id"]
            self.sent.append(did)
            return httpx.Response(200, json={"id": "m1", "threadId": "t1"})
        if req.method == "GET" and "/drafts/" in path:
            did = path.rsplit("/", 1)[1]
            if did not in self.drafts:
                return httpx.Response(404, json={"error": {"message": "Requested entity was not found."}})
            raw = base64.urlsafe_b64encode(self.drafts[did]).decode().rstrip("=")
            return httpx.Response(200, json={"id": did, "message": {"raw": raw}})
        if req.method == "DELETE":
            self.drafts.pop(path.rsplit("/", 1)[1], None)
            return httpx.Response(204)
        if req.url.host == "openidconnect.googleapis.com":
            return httpx.Response(200, json={"email": "me@example.com"})
        return httpx.Response(500)


def make(scopes=None, email=None, google=None):
    google = google or FakeGoogle()
    http = httpx.Client(transport=httpx.MockTransport(google))
    tok = StoredToken("cid", "csec", "RT", list(scopes if scopes is not None else SCOPES), email)
    sleeps: list[float] = []
    c = RealGmailClient(TokenProvider(tok, http), http, sleep=sleeps.append)
    return c, google, sleeps


def err(status, reason="", headers=None, message=""):
    return httpx.Response(status, json={"error": {"message": message, "errors": [{"reason": reason}]}}, headers=headers)


def test_token_refresh_once_and_cached():
    c, g, _ = make()
    d = c.create_draft(["a@example.com"], [], "S", "b")
    c.get_draft(d)
    assert g.token_calls == 1
    api = [r for r in g.requests if r.url.host != "oauth2.googleapis.com"]
    assert all(r.headers["Authorization"] == "Bearer AT1" for r in api)
    form = [r for r in g.requests if r.url.host == "oauth2.googleapis.com"][0].content.decode()
    assert "grant_type=refresh_token" in form and "refresh_token=RT" in form


def test_create_get_delete_send_roundtrip():
    c, g, _ = make()
    d = c.create_draft(["a@example.com"], ["b@example.com"], "Sübject", "body", "<p>b</p>", [("a.pdf", b"%PDF", "application/pdf")])
    up = g.requests[-1]
    assert up.url.path == "/upload/gmail/v1/users/me/drafts" and up.headers["content-type"] == "message/rfc822"
    draft = c.get_draft(d)
    assert draft.content.subject == "Sübject" and draft.content.attachments[0].data == b"%PDF"
    appr = ApprovedMessage(d, draft.sha256, "t", datetime.now(UTC))
    assert c.send_draft(appr) == ("m1", "t1")
    assert g.sent == [d]
    c.delete_draft(d)
    assert d not in g.drafts


def test_send_refuses_on_hash_mismatch_and_never_posts():
    c, g, _ = make()
    d = c.create_draft(["a@example.com"], [], "S", "body")
    appr = ApprovedMessage(d, c.get_draft(d).sha256, "t", datetime.now(UTC))
    g.drafts[d] = build_message(normalise(["a@example.com"], [], "S", "edited", None, []))
    with pytest.raises(ApprovalMismatch):
        c.send_draft(appr)
    assert g.sent == [] and not any(r.url.path.endswith("/drafts/send") for r in g.requests)


def test_profile_email_from_token_or_userinfo():
    c, _, _ = make(email="tok@example.com")
    assert c.profile_email() == "tok@example.com"
    c2, g, _ = make()
    assert c2.profile_email() == "me@example.com"


def test_401_refreshes_once_then_succeeds():
    c, g, _ = make()
    g.script = [err(401)]
    c.create_draft(["a@example.com"], [], "S", "b")
    assert g.token_calls == 2


def test_persistent_401_asks_reauth():
    c, g, _ = make()
    g.script = [err(401), err(401)]
    with pytest.raises(GmailAuthError, match="google-auth"):
        c.create_draft(["a@example.com"], [], "S", "b")


def test_refresh_invalid_grant_message():
    c, g, _ = make()
    g.token_response = httpx.Response(400, json={"error": "invalid_grant"})
    with pytest.raises(GmailAuthError, match="revoked or has expired.*google-auth"):
        c.profile_email()


def test_403_insufficient_scope():
    c, g, _ = make()
    g.script = [err(403, "insufficientPermissions", message="Request had insufficient authentication scopes.")]
    with pytest.raises(GmailScopeError, match="google-auth"):
        c.create_draft(["a@example.com"], [], "S", "b")


def test_missing_scope_in_token_fails_before_network():
    c, g, _ = make(scopes=["openid"])
    with pytest.raises(GmailScopeError, match="google-auth"):
        c.create_draft(["a@example.com"], [], "S", "b")
    assert g.requests == []
    c2, _, _ = make(scopes=[SCOPE_COMPOSE])
    with pytest.raises(GmailScopeError):
        c2.list_sent_since(datetime.now(UTC))


def test_429_and_5xx_retry_with_backoff():
    c, g, sleeps = make()
    g.script = [err(429, headers={"Retry-After": "3"}), err(503), err(500)]
    assert c.create_draft(["a@example.com"], [], "S", "b").startswith("d")
    assert sleeps == [3.0, 2.0, 4.0]


def test_retries_exhausted_is_transient():
    c, g, sleeps = make()
    g.script = [err(503)] * 4
    with pytest.raises(GmailTransientError):
        c.delete_draft("x")
    assert len(sleeps) == 3


def test_rate_limit_403_is_retried():
    c, g, _ = make()
    g.script = [err(403, "userRateLimitExceeded")]
    c.create_draft(["a@example.com"], [], "S", "b")


def test_send_not_retried_on_5xx():
    c, g, _ = make()
    d = c.create_draft(["a@example.com"], [], "S", "b")
    appr = ApprovedMessage(d, c.get_draft(d).sha256, "t", datetime.now(UTC))
    raw = base64.urlsafe_b64encode(g.drafts[d]).decode().rstrip("=")
    g.script = [httpx.Response(200, json={"id": d, "message": {"raw": raw}}), err(503)]  # GET ok, send 503
    with pytest.raises(GmailTransientError):
        c.send_draft(appr)
    assert sum(r.url.path.endswith("/drafts/send") for r in g.requests) == 1


def test_404_and_other_errors():
    c, g, _ = make()
    with pytest.raises(GmailNotFound):
        c.get_draft("nope")
    g.script = [err(400, message="Invalid To header")]
    with pytest.raises(GmailApiError, match="400"):
        c.delete_draft("x")


def test_network_error_is_transient_after_retries():
    def boom(req):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "A", "expires_in": 3600})
        raise httpx.ConnectError("down")

    http = httpx.Client(transport=httpx.MockTransport(boom))
    c = RealGmailClient(TokenProvider(StoredToken("c", "s", "r", list(SCOPES)), http), http, sleep=lambda s: None)
    with pytest.raises(GmailTransientError):
        c.delete_draft("x")
