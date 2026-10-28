"""Per-user login (bootstrap admin via SALES0_APP_PASSWORD) end to end (TestClient, real app + migrated SQLite)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from sales0.config import reset_settings
from sales0.web import auth
from sales0.web.app import create_app
from tests.integration.web.conftest import HX

PASSWORD = "s3cret-team-pass"
ADMIN_EMAIL = "admin@smalltech.in"
EVIL = "https://evil.example"


@pytest.fixture
def make_client(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., TestClient]]:
    opened: list[TestClient] = []

    def _make(password: str | None = PASSWORD, **env: str) -> TestClient:
        if password is None:
            monkeypatch.delenv("SALES0_APP_PASSWORD", raising=False)
        else:
            monkeypatch.setenv("SALES0_APP_PASSWORD", password)
        monkeypatch.setenv("SALES0_SECRET_KEY", "integration-test-secret")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        reset_settings()
        c = TestClient(create_app(), follow_redirects=False)
        c.__enter__()
        opened.append(c)
        return c

    yield _make
    for c in opened:
        c.__exit__(None, None, None)
    reset_settings()


@pytest.fixture
def locked(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


def _login(c: TestClient, password: str = PASSWORD, next_: str = "", **kw):
    return c.post("/login", data={"email": ADMIN_EMAIL, "password": password, "next": next_}, **kw)


# ---------------------------------------------------------------------- gate
def test_page_redirects_to_login_with_next(locked: TestClient) -> None:
    r = locked.get("/inbox")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Finbox"
    r = locked.get("/inbox?tab=skipped&q=a b")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Finbox%3Ftab%3Dskipped%26q%3Da%2520b"


def test_login_page_renders_without_chrome(locked: TestClient) -> None:
    r = locked.get("/login?next=/pipeline")
    assert r.status_code == 200
    for hook in ('id="login-form"', 'id="login-password"', 'id="login-submit"', 'value="/pipeline"', 'autocomplete="current-password"'):
        assert hook in r.text
    assert "site-nav" not in r.text and 'id="logout"' not in r.text
    assert PASSWORD not in r.text


def test_wrong_password_401_no_cookie(locked: TestClient) -> None:
    r = _login(locked, "nope", "/inbox")
    assert r.status_code == 401 and "Wrong email or password." in r.text
    assert "set-cookie" not in r.headers
    assert locked.get("/inbox").status_code == 303
    assert "nope" not in r.text


def test_empty_password_is_wrong(locked: TestClient) -> None:
    r = _login(locked, "")
    assert r.status_code == 401 and "set-cookie" not in r.headers
    assert locked.post("/login", data={"email": ADMIN_EMAIL}).status_code == 401


def test_correct_password_sets_cookie_and_next_works(locked: TestClient) -> None:
    r = _login(locked, next_="/pipeline?tab=leads")
    assert r.status_code == 303 and r.headers["location"] == "/pipeline?tab=leads"
    cookie = r.headers["set-cookie"].lower()
    assert "sales0_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert PASSWORD not in r.headers["set-cookie"]
    page = locked.get("/inbox")
    assert page.status_code == 200
    assert 'id="logout"' in page.text and 'action="/logout"' in page.text
    assert locked.get("/pipeline").status_code == 200


def test_default_next_is_inbox(locked: TestClient) -> None:
    assert _login(locked).headers["location"] == "/inbox"


@pytest.mark.parametrize("evil", ["https://evil.example", "//evil.example", "/\\evil.example", "javascript:alert(1)", "http:evil.example"])
def test_open_redirect_attempts_land_on_inbox(locked: TestClient, evil: str) -> None:
    r = _login(locked, next_=evil)
    assert r.status_code == 303 and r.headers["location"] == "/inbox"
    page = locked.get("/login", params={"next": evil})  # already logged in -> safe redirect
    assert page.status_code == 303 and page.headers["location"] == "/inbox"


def test_login_page_never_echoes_offsite_next(locked: TestClient) -> None:
    r = locked.get("/login", params={"next": "//evil.example"})
    assert "evil.example" not in r.text and 'value="/inbox"' in r.text
    r = locked.get("/login", params={"next": '/"><script>alert(1)</script>'})
    assert "<script>alert(1)" not in r.text


def test_logged_in_login_page_redirects(locked: TestClient) -> None:
    _login(locked)
    r = locked.get("/login?next=/runs")
    assert r.status_code == 303 and r.headers["location"] == "/runs"


# ---------------------------------------------------------------------- cookie validity
def test_tampered_cookie_rejected(locked: TestClient) -> None:
    _login(locked)
    value = locked.cookies[auth.COOKIE]
    locked.cookies.clear()
    locked.cookies.set(auth.COOKIE, value[:-2] + ("AA" if not value.endswith("AA") else "BB"))
    assert locked.get("/inbox").status_code == 303
    locked.cookies.set(auth.COOKIE, "v1:1:abc.def", path="/")
    assert locked.get("/inbox").status_code == 303


def test_expired_cookie_rejected(locked: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _login(locked)
    assert locked.get("/inbox").status_code == 200
    real = auth.now
    monkeypatch.setattr(auth, "now", lambda: real() + timedelta(days=15))
    r = locked.get("/inbox")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_env_password_is_bootstrap_only(make_client: Callable[..., TestClient]) -> None:
    first = make_client("old-password")
    assert _login(first, "old-password").status_code == 303
    cookie = first.cookies[auth.COOKIE]
    # the admin now has a password: a different env value is ignored for login, and the old session lives on
    second = make_client("new-password")
    assert _login(second, "new-password").status_code == 401
    assert _login(second, "old-password").status_code == 303
    second.cookies.set(auth.COOKIE, cookie)
    assert second.get("/inbox").status_code == 200


def test_changing_own_password_kills_other_sessions(make_client: Callable[..., TestClient]) -> None:
    a = make_client("old-password-1")
    _login(a, "old-password-1")
    b = TestClient(a.app, follow_redirects=False)
    _login(b, "old-password-1")
    r = a.post("/account/password", data={"current": "old-password-1", "password": "brand-new-pass-2", "confirm": "brand-new-pass-2"})
    assert r.status_code == 303
    assert a.get("/inbox").status_code == 200  # the session that changed it stays
    assert b.get("/inbox").status_code == 303  # the other is gone
    assert _login(b, "old-password-1").status_code == 401
    assert _login(b, "brand-new-pass-2").status_code == 303


# ---------------------------------------------------------------------- logout
def test_logout_clears_cookie_and_keeps_acting_cookie(locked: TestClient) -> None:
    _login(locked)
    locked.cookies.set("sales0_acting", "1", path="/")
    r = locked.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "sales0_session=" in r.headers["set-cookie"] and "max-age=0" in r.headers["set-cookie"].lower()
    assert locked.get("/inbox").status_code == 303
    assert locked.cookies.get("sales0_acting") == "1"


def test_logout_without_session_is_harmless(locked: TestClient) -> None:
    r = locked.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"


# ---------------------------------------------------------------------- open paths and caller kinds
def test_health_and_static_open(locked: TestClient) -> None:
    assert locked.get("/api/health").status_code == 200
    assert locked.get("/static/css/app.css").status_code == 200


def test_lookalike_paths_are_not_open(locked: TestClient) -> None:
    for path in ("/loginx", "/api/healthz", "/staticfoo"):
        assert locked.get(path).status_code in (303, 401), path


def test_htmx_gets_401_with_hx_redirect(locked: TestClient) -> None:
    r = locked.get("/inbox", headers=HX)
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/login" and r.text == ""
    r = locked.post("/runs", headers=HX)
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/login"


def test_json_callers_get_envelope(locked: TestClient) -> None:
    for r in (locked.get("/api/runs/1"), locked.get("/inbox", headers={"Accept": "application/json"})):
        assert r.status_code == 401
        assert r.json() == {"ok": False, "error": {"code": "unauthorized", "message": "Please log in."}}


def test_plain_form_post_redirects_to_login(locked: TestClient) -> None:
    r = locked.post("/keywords", data={"text": "x"})
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_logged_in_json_and_htmx_work(locked: TestClient) -> None:
    _login(locked)
    assert locked.get("/api/health").json()["ok"] is True
    assert locked.get("/runs", headers=HX).status_code == 200


def test_acting_as_selector_keeps_working(locked: TestClient) -> None:
    _login(locked)
    page = locked.get("/inbox")
    assert 'id="acting-as"' in page.text and 'id="logout"' in page.text
    r = locked.post("/team/acting", data={"member_id": "1", "next": "/inbox"})
    assert r.status_code == 303
    assert locked.get("/inbox").status_code == 200


def test_fixture_paths_gated_in_live_mode(locked: TestClient) -> None:
    # live mode never mounts the fixture LinkedIn, and the gate must not open the prefix either
    r = locked.get("/__fixtures/linkedin/feed/")
    assert r.status_code == 303


def test_fixture_paths_open_in_fixture_mode(make_client: Callable[..., TestClient]) -> None:
    c = make_client(SALES0_LINKEDIN_MODE="fixture")
    assert c.get("/__fixtures/linkedin/feed/").status_code != 303
    assert c.get("/inbox").status_code == 303


# ---------------------------------------------------------------------- throttle
def test_sixth_wrong_attempt_is_locked_even_with_right_password(locked: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    for _ in range(5):
        assert _login(locked, "bad").status_code == 401
    r = _login(locked, "bad")
    assert r.status_code == 429 and "Too many attempts - try again in" in r.text and "seconds." in r.text
    assert r.headers["retry-after"]
    r = _login(locked)  # right password, still locked
    assert r.status_code == 429 and "set-cookie" not in r.headers
    real = auth.now
    monkeypatch.setattr(auth, "now", lambda: real() + timedelta(seconds=61))
    r = _login(locked)
    assert r.status_code == 303 and "sales0_session=" in r.headers["set-cookie"]


def test_success_resets_failure_counter(locked: TestClient) -> None:
    for _ in range(4):
        _login(locked, "bad")
    assert _login(locked).status_code == 303
    for _ in range(4):
        assert _login(locked, "bad").status_code == 401


def test_x_forwarded_for_not_trusted(locked: TestClient) -> None:
    for i in range(5):
        _login(locked, "bad", headers={"X-Forwarded-For": f"10.0.0.{i}"})
    assert _login(locked, "bad", headers={"X-Forwarded-For": "10.9.9.9"}).status_code == 429


def test_throttle_is_per_ip_and_email(locked: TestClient) -> None:
    for _ in range(5):
        locked.post("/login", data={"email": "someone@else.example", "password": "bad"})
    assert locked.post("/login", data={"email": "someone@else.example", "password": "bad"}).status_code == 429
    assert _login(locked).status_code == 303  # a different email from the same address is not locked


def test_password_not_logged(locked: TestClient) -> None:
    import structlog

    with structlog.testing.capture_logs() as logs:
        _login(locked, "leaky-password-xyz")
    assert "leaky-password-xyz" not in repr(logs)


# ---------------------------------------------------------------------- CSRF
def test_cross_origin_login_post_rejected(locked: TestClient) -> None:
    r = locked.post("/login", data={"email": ADMIN_EMAIL, "password": PASSWORD}, headers={"Origin": EVIL})
    assert r.status_code == 403 and "set-cookie" not in r.headers
    r = locked.post("/login", data={"email": ADMIN_EMAIL, "password": PASSWORD}, headers={"Origin": EVIL, "Accept": "application/json"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_rejected"
    r = locked.post("/login", data={"email": ADMIN_EMAIL, "password": PASSWORD}, headers={"Referer": f"{EVIL}/x"})
    assert r.status_code == 403


def test_cross_origin_logout_rejected(locked: TestClient) -> None:
    _login(locked)
    assert locked.post("/logout", headers={"Origin": EVIL}).status_code == 403
    assert locked.get("/inbox").status_code == 200


def test_same_origin_login_post_allowed(locked: TestClient) -> None:
    from sales0.config import get_settings

    origin = f"http://127.0.0.1:{get_settings().port}"
    r = _login(locked, headers={"Origin": origin, "Referer": f"{origin}/login"})
    assert r.status_code == 303 and "sales0_session=" in r.headers["set-cookie"]


def test_public_base_url_origin_accepted(make_client: Callable[..., TestClient]) -> None:
    c = make_client(SALES0_PUBLIC_BASE_URL="https://crm.example.com")
    r = _login(c, headers={"Origin": "https://crm.example.com"})
    assert r.status_code == 303
    assert "; secure" in r.headers["set-cookie"].lower()
    assert _login(c, headers={"Origin": "https://other.example.com"}).status_code == 403


# ---------------------------------------------------------------------- password unset
def test_no_password_everything_open(make_client: Callable[..., TestClient]) -> None:
    c = make_client(None)
    for path in ("/inbox", "/runs", "/setup", "/pipeline", "/contacts"):
        assert c.get(path).status_code == 200, path
    assert c.get("/api/runs/1").status_code != 401
    r = c.get("/login")
    assert r.status_code == 303 and r.headers["location"] == "/"
    r = _login(c, "anything")
    assert r.status_code == 303 and r.headers["location"] == "/" and "set-cookie" not in r.headers
    page = c.get("/inbox")
    assert 'id="logout"' not in page.text
    assert c.get("/inbox", headers=HX).status_code == 200


def test_empty_password_treated_as_unset(make_client: Callable[..., TestClient]) -> None:
    c = make_client("")
    assert c.get("/inbox").status_code == 200
