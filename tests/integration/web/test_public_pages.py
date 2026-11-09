"""SMA-8 public front page (sales0, the AI-native CRM, at sales0.smalltech.in): open to anonymous visitors without
weakening the login gate on the internal app (real app + migrated SQLite, TestClient)."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from sales0.config import reset_settings
from sales0.web.app import create_app
from sales0.web.routes.public import ACCESS_URL

PASSWORD = "s3cret-team-pass"
ADMIN_EMAIL = "admin@smalltech.in"


@pytest.fixture
def make_client(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., TestClient]]:
    opened: list[TestClient] = []

    def _make(*, locked: bool) -> TestClient:
        if locked:
            monkeypatch.setenv("SALES0_APP_PASSWORD", PASSWORD)
        else:
            monkeypatch.delenv("SALES0_APP_PASSWORD", raising=False)
        monkeypatch.setenv("SALES0_SECRET_KEY", "integration-test-secret")
        reset_settings()
        c = TestClient(create_app(), follow_redirects=False)
        c.__enter__()
        opened.append(c)
        return c

    yield _make
    for c in opened:
        c.__exit__(None, None, None)
    reset_settings()


def _assert_landing(html: str) -> None:
    assert "AI-native CRM" in html and "small sales teams." in html
    assert f'href="{ACCESS_URL}"' in html and "mailto:contact@smalltech.in" in html
    assert "Zer0 Sales Agent" not in html and "/pricing" not in html  # the chat agent moved to chatbot0
    assert '<link rel="canonical" href="https://sales0.smalltech.in/">' in html
    assert 'property="og:title"' in html and 'name="twitter:card"' in html
    assert "/static/img/logo-smalltech.svg" in html and "fonts.googleapis.com" in html
    # no internal chrome or data on a public page
    assert "site-nav" not in html and "status-light" not in html and "htmx" not in html


@pytest.mark.parametrize("path", ["/", "/sales-agent", "/robots.txt", "/sitemap.xml", "/static/css/public.css", "/static/img/logo-smalltech.svg"])
def test_public_pages_open_without_login(make_client: Callable[..., TestClient], path: str) -> None:
    c = make_client(locked=True)
    r = c.get(path)
    assert r.status_code == 200, path
    assert "set-cookie" not in r.headers


def test_anonymous_root_is_the_landing_and_its_csp_allows_only_google_fonts(make_client: Callable[..., TestClient]) -> None:
    c = make_client(locked=True)
    r = c.get("/")
    _assert_landing(r.text)
    csp = r.headers["content-security-policy"]
    assert "script-src 'none'" in csp and "font-src https://fonts.gstatic.com" in csp and "frame-ancestors 'none'" in csp
    assert r.headers.get_list("content-security-policy") == [csp]  # the app default was not appended on top
    assert c.get("/pricing").status_code == 303  # no public pricing page: behind the login gate like any internal path


def test_internal_routes_still_require_login_when_accounts_are_configured(make_client: Callable[..., TestClient]) -> None:
    c = make_client(locked=True)
    for path in ("/inbox", "/pipeline", "/setup", "/users", "/sales-agentx", "/pricing/x", "/robots.txtx"):
        r = c.get(path)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path
    assert c.get("/api/pipeline/summary").status_code == 401
    assert c.post("/runs", data={}).status_code == 303  # unsafe methods on internal paths are gated too


def test_signed_in_member_on_root_still_goes_to_the_inbox(make_client: Callable[..., TestClient]) -> None:
    c = make_client(locked=True)
    assert c.post("/login", data={"email": ADMIN_EMAIL, "password": PASSWORD, "next": ""}).status_code == 303
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/inbox"
    assert c.get("/sales-agent").status_code == 200  # the landing stays reachable for previews


def test_open_mode_root_still_redirects_to_inbox_and_landing_is_previewable(make_client: Callable[..., TestClient]) -> None:
    c = make_client(locked=False)
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/inbox"
    _assert_landing(c.get("/sales-agent").text)
