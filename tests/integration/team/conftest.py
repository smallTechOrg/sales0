"""Team/OAuth fixtures: a loopback TestClient, a fake Google (httpx.MockTransport) and helpers."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from sales0.config import reset_settings
from sales0.integrations.gmail.auth import SCOPES
from sales0.web.app import create_app

ADMIN_EMAIL = "admin@team.example"
REFRESH = "1//plaintext-refresh-token-ZZZ"
BASE = "http://127.0.0.1:8766"  # TestClient never connects; only the Host header matters


@dataclass
class FakeGoogle:
    email: str = ADMIN_EMAIL
    refresh_token: str | None = REFRESH
    scope: str = " ".join(SCOPES)
    token_status: int = 200
    calls: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = {k: v[0] for k, v in parse_qs(request.content.decode()).items()} if request.content else {}
        self.calls.append((request.method, request.url.host + request.url.path, body))
        if request.url.path == "/token":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            data = {"access_token": "ya29.access", "expires_in": 3600, "scope": self.scope, "token_type": "Bearer"}
            if self.refresh_token:
                data["refresh_token"] = self.refresh_token
            return httpx.Response(200, json=data)
        if request.url.path == "/v1/userinfo":
            return httpx.Response(200, json={"email": self.email})
        if request.url.path == "/revoke":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [c[1] for c in self.calls]


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def client_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "client_secret.json"
    path.write_text(json.dumps({"installed": {"client_id": "cid.apps.example", "client_secret": "csecret"}}))
    monkeypatch.setenv("SALES0_GOOGLE_CLIENT_SECRET_PATH", str(path))
    return path


@pytest.fixture
def client(
    db_url: str, google: FakeGoogle, client_secret: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("SALES0_ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("SALES0_ADMIN_NAME", "Ada Admin")
    monkeypatch.setenv("SALES0_SECRET_KEY", "test-secret-key-0123456789abcdef")
    monkeypatch.setenv("SALES0_GOOGLE_TOKEN_ENDPOINT", "https://oauth.fake/token")
    monkeypatch.setenv("SALES0_GOOGLE_USERINFO_ENDPOINT", "https://oauth.fake/v1/userinfo")
    monkeypatch.setenv("SALES0_GOOGLE_REVOKE_ENDPOINT", "https://oauth.fake/revoke")
    reset_settings()
    app = create_app()
    app.state.oauth_http_factory = lambda: httpx.Client(transport=httpx.MockTransport(google.handler))
    with TestClient(app, base_url=BASE) as c:
        yield c


def start_connect(client: TestClient) -> dict[str, str]:
    """POST connect; return the query params of the Google URL the user is sent to."""
    resp = client.post("/team/gmail/connect", follow_redirects=False)
    assert resp.status_code == 303, resp.text
    parts = urlsplit(resp.headers["location"])
    assert parts.netloc == "accounts.google.com"
    return {k: v[0] for k, v in parse_qs(parts.query).items()}
