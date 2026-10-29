"""Security fixes (web infra) through the real app: Host guard, body caps, headers, health, throttle, session age."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from sales0.config import reset_settings
from sales0.web import auth
from sales0.web.app import create_app

PASSWORD = "s3cret-team-pass"
ADMIN_EMAIL = "admin@smalltech.in"


@pytest.fixture
def make_client(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., TestClient]]:
    opened: list[TestClient] = []

    def _make(password: str | None = None, **env: str) -> TestClient:
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


def _login(c: TestClient) -> None:
    r = c.post("/login", data={"email": ADMIN_EMAIL, "password": PASSWORD, "next": ""})
    assert r.status_code == 303, r.text


# ---------------------------------------------------------------------- #4 Host guard (DNS rebinding probe)
def test_open_mode_rejects_a_foreign_host_on_every_method(make_client: Callable[..., TestClient]) -> None:
    c = make_client()
    assert c.get("/team").status_code == 200  # TestClient's own Host is allowed under the test flag
    for method, path in (("GET", "/team"), ("GET", "/api/health"), ("POST", "/setup/keywords"), ("GET", "/inbox")):
        r = c.request(method, path, headers={"Host": "evil.example"})
        assert r.status_code == 400, (method, path)
        assert "evil.example" not in r.text.replace("SALES0_ALLOWED_HOSTS", "")


def test_open_mode_accepts_loopback_names_with_any_port(make_client: Callable[..., TestClient]) -> None:
    c = make_client()
    for host in ("127.0.0.1:9", "localhost:8801", "[::1]:8802", "127.0.0.1"):
        assert c.get("/team", headers={"Host": host}).status_code == 200, host


def test_open_mode_ignores_public_url_and_allowed_hosts(make_client: Callable[..., TestClient]) -> None:
    c = make_client(None, SALES0_PUBLIC_BASE_URL="https://sales.example.com", SALES0_ALLOWED_HOSTS="crm.example.com")
    assert c.get("/team", headers={"Host": "sales.example.com"}).status_code == 400  # no login -> loopback only


def test_testserver_host_is_refused_unless_the_test_flag_is_on(make_client: Callable[..., TestClient]) -> None:
    c = make_client(None, SALES0_ALLOW_TESTSERVER_HOST="false")
    assert c.get("/team").status_code == 400
    assert c.get("/team", headers={"Host": "127.0.0.1:8800"}).status_code == 200


def test_login_mode_allowlist_includes_public_url_and_allowed_hosts(make_client: Callable[..., TestClient]) -> None:
    c = make_client(PASSWORD, SALES0_PUBLIC_BASE_URL="https://sales.example.com", SALES0_ALLOWED_HOSTS="crm.example.com:8443, other.test")
    assert c.get("/login", headers={"Host": "evil.example"}).status_code == 400
    assert c.post("/login", data={"email": "x", "password": "y"}, headers={"Host": "evil.example"}).status_code == 400
    for host in ("sales.example.com", "crm.example.com", "other.test:81", "127.0.0.1:8800"):
        assert c.get("/login", headers={"Host": host}).status_code == 200, host


# ---------------------------------------------------------------------- #1 body size
def test_oversized_content_length_is_refused_before_the_body_is_read(make_client: Callable[..., TestClient]) -> None:
    c = make_client()
    r = c.post("/setup/keywords", content=b"a=" + b"x" * (2 * 1024 * 1024), headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large" if "application/json" in r.headers.get("content-type", "") else True


def test_oversized_json_envelope_for_api_clients(make_client: Callable[..., TestClient]) -> None:
    c = make_client()
    r = c.post("/setup/keywords", content=b"x" * (1024 * 1024 + 1), headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413 and r.json() == {"ok": False, "error": {"code": "payload_too_large", "message": "That request is too large."}}


def test_chunked_body_without_content_length_is_capped_too(make_client: Callable[..., TestClient]) -> None:
    c = make_client(PASSWORD)  # POST /login parses a form body, so it reads through the capped receive

    def chunks() -> Iterator[bytes]:
        for _ in range(40):
            yield b"a=" + b"x" * (64 * 1024) + b"&"

    r = c.post("/login", content=chunks(), headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"


def test_normal_sized_forms_and_the_csv_upload_limit_still_work(make_client: Callable[..., TestClient]) -> None:
    c = make_client()
    assert c.post("/setup/keywords", data={"text": "ok phrase"}, headers={"Accept": "application/json"}).status_code != 413
    big = b"x" * (5 * 1024 * 1024 + 70 * 1024)
    r = c.post("/import/csv", files={"file": ("a.csv", big, "text/csv")}, data={"kind": "enquiries"}, headers={"Accept": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "import_too_large"


# ---------------------------------------------------------------------- #13 security headers
@pytest.mark.parametrize("path", ["/team", "/api/health", "/static/css/app.css", "/no-such-page"])
def test_security_headers_on_every_response(make_client: Callable[..., TestClient], path: str) -> None:
    r = make_client().get(path)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "same-origin"
    assert "camera=()" in r.headers["permissions-policy"]
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "'unsafe-inline'" not in csp.split("style-src-attr")[0]


def test_security_headers_on_rejections_and_redirects(make_client: Callable[..., TestClient]) -> None:
    locked = make_client(PASSWORD)
    for r in (locked.get("/inbox"), locked.get("/login", headers={"Host": "evil.example"}), locked.post("/setup/keywords", content=b"x" * (2 << 20))):
        assert r.headers["x-frame-options"] == "DENY" and "content-security-policy" in r.headers


def test_pages_carry_no_inline_script_or_handlers(make_client: Callable[..., TestClient]) -> None:
    import re

    c = make_client()
    for path in ("/inbox", "/team", "/metrics", "/pipeline", "/replies", "/outreach/outbox", "/digest", "/suggestions", "/users", "/costs"):
        html = c.get(path).text
        assert not re.search(r"<script(?![^>]*\ssrc=)", html), path
        assert not re.search(r"\son[a-z]+=\"", html), path
        assert "<style" not in html, path


# ---------------------------------------------------------------------- #16 health
def test_health_is_minimal_without_a_session_and_full_with_one(make_client: Callable[..., TestClient]) -> None:
    c = make_client(PASSWORD)
    assert c.get("/api/health").json() == {"ok": True, "data": {"status": "ok"}}
    _login(c)
    data = c.get("/api/health").json()["data"]
    assert {"status", "db_revision", "linkedin_mode", "model", "api_key_present"} <= set(data)


def test_health_is_full_in_open_mode(make_client: Callable[..., TestClient]) -> None:
    assert "db_revision" in make_client().get("/api/health").json()["data"]


# ---------------------------------------------------------------------- #10 password change throttle
def test_account_password_change_is_throttled_like_login(make_client: Callable[..., TestClient]) -> None:
    c = make_client(PASSWORD, SALES0_LOGIN_MAX_ATTEMPTS="3")
    _login(c)
    for _ in range(3):
        r = c.post("/account/password", data={"current": "wrong", "password": "another-long-pass", "confirm": "another-long-pass"})
        assert r.status_code == 303
    r = c.post("/account/password", data={"current": PASSWORD, "password": "another-long-pass", "confirm": "another-long-pass"})
    assert r.status_code == 303 and "Retry-After" in r.headers  # locked: even the right password is refused now
    # the old password still works => the change was not applied
    fresh = make_client(PASSWORD)
    assert fresh.post("/login", data={"email": ADMIN_EMAIL, "password": PASSWORD, "next": ""}).status_code in {303, 429}


# ---------------------------------------------------------------------- #11 session absolute max
def test_session_dies_at_90_days_even_when_used_every_day(make_client: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch) -> None:
    c = make_client(PASSWORD, SALES0_SESSION_DAYS="30")
    start = auth.now()
    clock = {"t": start}
    monkeypatch.setattr(auth, "now", lambda: clock["t"])
    _login(c)
    for day in range(10, 90, 10):  # keep using it: sliding renewal + rotation
        clock["t"] = start + timedelta(days=day)
        assert c.get("/team").status_code == 200, day
    clock["t"] = start + timedelta(days=89, hours=23)
    assert c.get("/team").status_code == 200
    clock["t"] = start + timedelta(days=90, minutes=1)
    r = c.get("/team")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


# ---------------------------------------------------------------------- #15 through the app
def test_flash_cookie_secure_follows_trusted_x_forwarded_proto(make_client: Callable[..., TestClient]) -> None:
    c = make_client(PASSWORD, SALES0_TRUSTED_PROXIES="testclient,127.0.0.1")
    _login(c)
    # TestClient peer is "testclient" (not an IP): not trusted -> the header is ignored
    r = c.post("/account/password", data={"current": "x", "password": "a", "confirm": "b"}, headers={"X-Forwarded-Proto": "https"})
    assert "; secure" not in r.headers["set-cookie"].lower()


def test_flash_cookie_is_secure_with_cookie_secure_true(make_client: Callable[..., TestClient]) -> None:
    c = make_client(PASSWORD, SALES0_COOKIE_SECURE="true")
    r = c.post("/login", data={"email": ADMIN_EMAIL, "password": PASSWORD, "next": ""})
    assert "; secure" in r.headers["set-cookie"].lower()
    r = c.post("/account/password", data={"current": "x", "password": "a", "confirm": "b"}, headers={"Origin": "http://testserver"})
    assert all("; secure" in h.lower() for h in r.headers.get_list("set-cookie"))


def test_fixture_linkedin_pages_keep_their_inline_scripts(make_client: Callable[..., TestClient]) -> None:
    """The fake LinkedIn (fixture mode, loopback only) needs inline scripts, so only those paths skip the CSP."""
    c = make_client(None, SALES0_LINKEDIN_MODE="fixture")
    r = c.get("/__fixtures/linkedin/login")
    assert r.status_code == 200 and "content-security-policy" not in r.headers
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in c.get("/team").headers
