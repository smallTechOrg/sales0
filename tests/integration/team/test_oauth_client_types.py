"""Desktop ('installed') and Web client JSONs through the per-member OAuth flow, against the fake Google."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from sales0.config import reset_settings
from sales0.integrations.gmail.auth import client_kind, client_redirect_uris, read_client_secret
from sales0.integrations.gmail.errors import GmailNotConfigured
from sales0.web.app import create_app
from tests.support.fake_google import FakeGoogle, serve_fake_google

ADMIN = "admin@team.example"
PUBLIC = "https://sales.example.com"
CALLBACK = "/oauth/google/callback"
LOCAL = "http://127.0.0.1:8766"


@pytest.fixture
def fake_google() -> Iterator[FakeGoogle]:
    with serve_fake_google(ADMIN) as fake:
        yield fake


@pytest.fixture
def make_client(
    db_url: str, tmp_path: Path, fake_google: FakeGoogle, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def make(secret: dict, *, public_base_url: str | None = None, base_url: str = LOCAL) -> TestClient:
        path = tmp_path / "client_secret.json"
        path.write_text(json.dumps(secret))
        monkeypatch.setenv("SALES0_GOOGLE_CLIENT_SECRET_PATH", str(path))
        monkeypatch.setenv("SALES0_ADMIN_EMAIL", ADMIN)
        monkeypatch.setenv("SALES0_SECRET_KEY", "test-secret-key-0123456789abcdef")
        for key, value in fake_google.env().items():
            monkeypatch.setenv(key, value)
        if public_base_url:
            monkeypatch.setenv("SALES0_PUBLIC_BASE_URL", public_base_url)
        reset_settings()
        c = TestClient(create_app(), base_url=base_url)
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)
    reset_settings()


def _flow(client: TestClient) -> tuple[str, str]:
    """Connect -> fake Google authorize -> callback. Returns (redirect_uri used, resulting team page)."""
    resp = client.post("/team/gmail/connect", follow_redirects=False)
    assert resp.status_code == 303
    google_url = resp.headers["location"]
    q = {k: v[0] for k, v in parse_qs(urlsplit(google_url).query).items()}
    import httpx

    back = httpx.get(google_url, follow_redirects=False)  # the fake Google "Allow" redirect
    assert back.status_code == 302
    target = urlsplit(back.headers["location"])
    cb = client.get(f"{CALLBACK}?{target.query}", follow_redirects=False)
    assert cb.status_code == 303
    return q["redirect_uri"], client.get("/team").text


def test_desktop_client_uses_loopback_redirect(make_client: Callable[..., TestClient]) -> None:
    c = make_client({"installed": {"client_id": "cid", "client_secret": "cs"}})
    uri, page = _flow(c)
    assert uri == LOCAL + CALLBACK
    assert f"Gmail connected as {ADMIN}" in page


def test_web_client_with_public_base_url_completes(
    make_client: Callable[..., TestClient], fake_google: FakeGoogle
) -> None:
    c = make_client(
        {"web": {"client_id": "cid", "client_secret": "cs", "redirect_uris": [PUBLIC + CALLBACK]}},
        public_base_url=PUBLIC,
    )
    uri, page = _flow(c)
    assert uri == PUBLIC + CALLBACK
    assert f"Gmail connected as {ADMIN}" in page
    assert "/token" in fake_google.paths()


def test_web_client_without_redirect_list_is_not_pre_validated(make_client: Callable[..., TestClient]) -> None:
    c = make_client({"web": {"client_id": "cid", "client_secret": "cs"}}, public_base_url=PUBLIC)
    uri, _ = _flow(c)
    assert uri == PUBLIC + CALLBACK


def test_web_client_redirect_not_registered_names_the_exact_uri(make_client: Callable[..., TestClient]) -> None:
    c = make_client(
        {"web": {"client_id": "cid", "client_secret": "cs", "redirect_uris": ["https://other.example/x"]}},
        public_base_url=PUBLIC,
    )
    assert c.post("/team/gmail/connect", follow_redirects=False).headers["location"] == "/team"
    page = c.get("/team").text
    assert "not listed in the Google client JSON" in page
    assert PUBLIC + CALLBACK in page and "https://other.example/x" in page


def test_web_client_on_loopback_must_list_that_exact_port(make_client: Callable[..., TestClient]) -> None:
    c = make_client({"web": {"client_id": "cid", "client_secret": "cs", "redirect_uris": ["http://127.0.0.1:9999" + CALLBACK]}})
    c.post("/team/gmail/connect", follow_redirects=False)
    assert "not listed in the Google client JSON" in c.get("/team").text


def test_desktop_client_with_public_url_is_refused_with_advice(make_client: Callable[..., TestClient]) -> None:
    c = make_client({"installed": {"client_id": "cid", "client_secret": "cs"}}, public_base_url=PUBLIC)
    c.post("/team/gmail/connect", follow_redirects=False)
    page = c.get("/team").text
    assert "Desktop app client" in page and "Web application" in page


def test_team_page_shows_the_exact_redirect_uri_to_register(make_client: Callable[..., TestClient]) -> None:
    c = make_client({"web": {"client_id": "cid", "client_secret": "cs"}}, public_base_url=PUBLIC)
    page = c.get("/team").text
    assert 'id="gmail-redirect-hint"' in page and PUBLIC + CALLBACK in page


def test_client_json_helpers(tmp_path: Path) -> None:
    web = tmp_path / "w.json"
    web.write_text(json.dumps({"web": {"client_id": "a", "client_secret": "b", "redirect_uris": ["u1"]}}))
    assert client_kind(web) == "web" and client_redirect_uris(web) == ["u1"]
    assert read_client_secret(web)["client_id"] == "a"
    desktop = tmp_path / "d.json"
    desktop.write_text(json.dumps({"installed": {"client_id": "a", "client_secret": "b"}}))
    assert client_kind(desktop) == "installed" and client_redirect_uris(desktop) == []
    bad = tmp_path / "b.json"
    bad.write_text(json.dumps({"other": {}}))
    with pytest.raises(GmailNotConfigured, match="'installed' or 'web'"):
        read_client_secret(bad)
    bad.write_text("[1]")
    with pytest.raises(GmailNotConfigured):
        read_client_secret(bad)
