"""Shared login units: session cookie sign/verify/expiry/password change, throttle, safe ``next``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from starlette.requests import Request
from starlette.responses import Response

from sales0.config import Settings, get_settings
from sales0.web import auth

T0 = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def _settings(password: str | None = "hunter2", **extra: object) -> Settings:
    update = {"app_password": SecretStr(password) if password else None, "secret_key": SecretStr("unit-test-secret"), **extra}
    return get_settings().model_copy(update=update)


def _cookie_value(response: Response) -> str:
    header = response.headers["set-cookie"]
    return header.split(";", 1)[0].split("=", 1)[1]


def _request(cookie: str | None) -> Request:
    headers = [(b"cookie", f"{auth.COOKIE}={cookie}".encode())] if cookie else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "query_string": b""})


def _issued(settings: Settings, at: datetime = T0) -> str:
    response = Response()
    auth.issue_session(response, settings, at)
    return _cookie_value(response)


def test_cookie_attributes() -> None:
    response = Response()
    auth.issue_session(response, _settings(), T0)
    header = response.headers["set-cookie"].lower()
    assert header.startswith(f"{auth.COOKIE}=")
    assert "httponly" in header and "samesite=lax" in header and "path=/" in header
    assert f"max-age={14 * 86400}" in header
    assert "secure" not in header.replace("samesite", "")


def test_cookie_secure_on_https_public_url() -> None:
    response = Response()
    auth.issue_session(response, _settings(public_base_url="https://crm.example.com"), T0)
    assert "; secure" in response.headers["set-cookie"].lower()


def test_valid_session_roundtrip() -> None:
    s = _settings()
    assert auth.session_valid(_request(_issued(s)), s, T0 + timedelta(days=1))


def test_missing_and_garbage_cookie_rejected() -> None:
    s = _settings()
    for value in (None, "", "garbage", "a.b", "v1:1:2.x"):
        assert not auth.session_valid(_request(value), s, T0)


def test_tampered_cookie_rejected() -> None:
    s = _settings()
    value = _issued(s)
    flipped = value[:-1] + ("A" if value[-1] != "A" else "B")
    assert not auth.session_valid(_request(flipped), s, T0)
    body, _, sig = value.rpartition(".")
    forged = body.replace(":" + body.split(":")[1] + ":", ":9999999999:") + "." + sig
    assert not auth.session_valid(_request(forged), s, T0)


def test_cookie_signed_with_another_secret_or_purpose_rejected() -> None:
    a, b = _settings(), _settings(secret_key=SecretStr("other-secret"))
    assert not auth.session_valid(_request(_issued(a)), b, T0)
    from sales0.services.team import crypto

    other_purpose = crypto.sign(f"v1:{int(T0.timestamp())}:{auth.password_fingerprint(a)}", "cookie", a)
    assert not auth.session_valid(_request(other_purpose), a, T0)


def test_expiry_boundary() -> None:
    s = _settings()
    value = _issued(s)
    assert auth.session_valid(_request(value), s, T0 + timedelta(days=14) - timedelta(seconds=1))
    assert not auth.session_valid(_request(value), s, T0 + timedelta(days=14))
    assert not auth.session_valid(_request(value), _settings(session_days=1), T0 + timedelta(days=2))


def test_future_dated_cookie_rejected() -> None:
    s = _settings()
    assert not auth.session_valid(_request(_issued(s, T0 + timedelta(hours=1))), s, T0)


def test_changing_the_password_invalidates_sessions() -> None:
    value = _issued(_settings("old-password"))
    assert auth.session_valid(_request(value), _settings("old-password"), T0)
    assert not auth.session_valid(_request(value), _settings("new-password"), T0)


def test_password_matches_constant_time_semantics() -> None:
    s = _settings("correct horse")
    assert auth.password_matches(s, "correct horse")
    assert not auth.password_matches(s, "correct hors")
    assert not auth.password_matches(s, "")
    assert not auth.password_matches(_settings(None), "")


def test_fingerprint_does_not_contain_password() -> None:
    fp = auth.password_fingerprint(_settings("hunter2"))
    assert len(fp) == 12 and "hunter2" not in fp


@pytest.mark.parametrize(
    "raw",
    ["https://evil.example", "//evil.example", "///evil.example", "/\\evil.example", "http:evil.example", "javascript:alert(1)",
     "evil.example", "", None, "/ok\nSet-Cookie: x=1", "\\\\evil.example", "/login", "/logout"],
)
def test_safe_next_rejects_offsite(raw: str | None) -> None:
    assert auth.safe_next(raw) == "/inbox"


@pytest.mark.parametrize("raw", ["/inbox", "/pipeline?tab=leads&q=a%20b", "/contacts/12", "/"])
def test_safe_next_keeps_local_paths(raw: str) -> None:
    assert auth.safe_next(raw) == raw


def test_throttle_locks_after_max_failures_and_expires() -> None:
    t = auth.LoginThrottle(max_attempts=3, lockout_s=60)
    for i in range(2):
        t.fail("1.1.1.1", T0 + timedelta(seconds=i))
        assert t.check("1.1.1.1", T0 + timedelta(seconds=i)) is None
    t.fail("1.1.1.1", T0 + timedelta(seconds=2))
    assert t.check("1.1.1.1", T0 + timedelta(seconds=2)) == 60
    assert t.check("1.1.1.1", T0 + timedelta(seconds=22)) == 40
    assert t.check("2.2.2.2", T0 + timedelta(seconds=22)) is None  # per client
    assert t.check("1.1.1.1", T0 + timedelta(seconds=62)) is None
    t.fail("1.1.1.1", T0 + timedelta(seconds=63))  # counter restarted
    assert t.check("1.1.1.1", T0 + timedelta(seconds=63)) is None


def test_throttle_window_forgets_old_failures_and_success_resets() -> None:
    t = auth.LoginThrottle(max_attempts=3, lockout_s=60)
    t.fail("ip", T0)
    t.fail("ip", T0 + timedelta(seconds=1))
    t.fail("ip", T0 + timedelta(seconds=90))  # first two aged out of the 60 s window
    assert t.check("ip", T0 + timedelta(seconds=90)) is None
    t.fail("ip", T0 + timedelta(seconds=91))
    t.reset("ip")
    t.fail("ip", T0 + timedelta(seconds=92))
    assert t.check("ip", T0 + timedelta(seconds=92)) is None
