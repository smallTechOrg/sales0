"""Login units: session cookie attributes, client IP / trusted proxies, throttle, safe ``next``."""

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


def _request(headers: list[tuple[bytes, bytes]] | None = None, peer: str = "9.9.9.9") -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers or [], "query_string": b"", "client": (peer, 1234)})


def test_cookie_attributes_and_expiry() -> None:
    response = Response()
    auth.set_session_cookie(response, _settings(), "sid-value", T0 + timedelta(days=14), T0)
    header = response.headers["set-cookie"].lower()
    assert header.startswith(f"{auth.COOKIE}=")
    assert "httponly" in header and "samesite=lax" in header and "path=/" in header
    assert f"max-age={14 * 86400}" in header
    assert "secure" not in header.replace("samesite", "")
    assert "sid-value." in response.headers["set-cookie"]  # signed: value.signature


def test_cookie_secure_on_https_public_url() -> None:
    response = Response()
    auth.set_session_cookie(response, _settings(public_base_url="https://crm.example.com"), "x", T0 + timedelta(days=1), T0)
    assert "; secure" in response.headers["set-cookie"].lower()


def test_cookie_sid_rejects_tampering_and_other_purpose() -> None:
    from sales0.services.team import crypto

    s = _settings()
    good = crypto.sign("abc", auth.SESSION_PURPOSE, s)
    hdr = lambda v: [(b"cookie", f"{auth.COOKIE}={v}".encode())]  # noqa: E731
    assert auth.cookie_sid(_request(hdr(good)), s) == "abc"
    assert auth.cookie_sid(_request(hdr(good[:-2] + "xx")), s) is None
    assert auth.cookie_sid(_request(hdr(crypto.sign("abc", "cookie", s))), s) is None
    assert auth.cookie_sid(_request(hdr(good)), _settings(secret_key=SecretStr("other"))) is None
    assert auth.cookie_sid(_request(), s) is None


# ---------------------------------------------------------------------- client address / trusted proxies
def _xff(value: str | list[str], peer: str, trusted: str) -> str:
    values = [value] if isinstance(value, str) else value
    req = _request([(b"x-forwarded-for", v.encode()) for v in values], peer)
    return auth.client_ip(req, _settings(trusted_proxies=trusted))


def test_xff_ignored_without_trusted_proxies_or_from_untrusted_peer() -> None:
    assert _xff("1.2.3.4", "9.9.9.9", "") == "9.9.9.9"
    assert _xff("1.2.3.4", "9.9.9.9", "10.0.0.0/8") == "9.9.9.9"


def test_xff_rightmost_untrusted_entry_from_trusted_peer() -> None:
    assert _xff("1.2.3.4", "10.0.0.5", "10.0.0.0/8") == "1.2.3.4"
    # a client-forged left entry cannot win: the right-most untrusted hop is believed
    assert _xff("6.6.6.6, 1.2.3.4", "10.0.0.5", "10.0.0.0/8") == "1.2.3.4"
    assert _xff("6.6.6.6, 1.2.3.4, 10.0.0.9", "10.0.0.5", "10.0.0.0/8") == "1.2.3.4"
    assert _xff(["6.6.6.6", "1.2.3.4"], "10.0.0.5", "10.0.0.5") == "1.2.3.4"  # several header lines
    assert _xff("10.0.0.7, 10.0.0.9", "10.0.0.5", "10.0.0.0/8") == "10.0.0.7"  # all trusted: left-most


def test_xff_garbage_falls_back_to_peer() -> None:
    assert _xff("not-an-ip", "10.0.0.5", "10.0.0.0/8") == "10.0.0.5"
    assert _xff("1.2.3.4, junk", "10.0.0.5", "10.0.0.0/8") == "10.0.0.5"
    assert _xff("1.2.3.4", "10.0.0.5", "garbage, ,10.0.0.0/8") == "1.2.3.4"  # bad config entries are skipped


def test_xff_ipv6_and_mapped() -> None:
    assert _xff("2001:db8::1", "::1", "::1") == "2001:db8::1"
    assert _xff("1.2.3.4", "::ffff:10.0.0.5", "10.0.0.0/8") == "1.2.3.4"


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


def test_login_guard_is_per_pair_with_looser_ip_bucket() -> None:
    g = auth.LoginGuard(max_attempts=2, lockout_s=60)
    g.fail("1.1.1.1", "a@x.example", T0)
    g.fail("1.1.1.1", "A@x.example ", T0)  # emails are normalised
    assert g.check("1.1.1.1", "a@x.example", T0) == 60
    assert g.check("1.1.1.1", "b@x.example", T0) is None  # another email from the same IP is fine...
    for i in range(6):  # ...until the per-IP bucket (2 x 4 = 8 failures) fills by spraying many emails
        g.fail("1.1.1.1", f"u{i}@x.example", T0)
    assert g.check("1.1.1.1", "zzz@x.example", T0) == 60
    assert g.check("2.2.2.2", "a@x.example", T0) is None
    g.reset("1.1.1.1", "a@x.example")
    assert g.pair.check("1.1.1.1|a@x.example", T0) is None
