"""Security fixes (web infra): secret-key floor, cookie Secure tri-state, log redaction, storage modes, startup refusals."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from starlette.requests import Request
from starlette.responses import Response

from sales0.config import Settings, StartupError, check_startup, get_settings
from sales0.db.engine import harden_storage
from sales0.web import cookies
from sales0.web.envelope import redact_path
from sales0.web.middleware import DEFAULT_BODY_LIMIT, body_limit, security_headers


def _settings(**extra: object) -> Settings:
    return get_settings().model_copy(update=extra)


def _request(headers: dict[str, str] | None = None, peer: str = "10.0.0.2", scheme: str = "http") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/", "scheme": scheme, "headers": raw, "query_string": b"", "client": (peer, 1234)})


# ---------------------------------------------------------------------- #9 secret key floor
def test_short_secret_key_is_refused_with_a_clear_error() -> None:
    with pytest.raises(ValidationError, match="at least 16"):
        Settings(secret_key="too-short")  # type: ignore[arg-type]


def test_secret_key_16_chars_or_a_fernet_key_is_accepted_and_blank_is_unset() -> None:
    from cryptography.fernet import Fernet

    assert Settings(secret_key="x" * 16).secret_key is not None  # type: ignore[arg-type]
    assert Settings(secret_key=Fernet.generate_key().decode()).secret_key is not None  # type: ignore[arg-type]
    assert Settings(secret_key="   ").secret_key is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------- #15 cookie Secure tri-state
def test_cookie_secure_forced_true_and_false() -> None:
    assert cookies.cookie_secure(_request(), _settings(cookie_secure="true")) is True
    assert cookies.cookie_secure(_request(scheme="https"), _settings(cookie_secure="false")) is False


def test_cookie_secure_auto_follows_the_request_scheme() -> None:
    s = _settings(cookie_secure="auto", public_base_url=None, trusted_proxies="")
    assert cookies.cookie_secure(_request(), s) is False
    assert cookies.cookie_secure(_request(scheme="https"), s) is True


def test_cookie_secure_auto_trusts_x_forwarded_proto_only_from_a_trusted_proxy() -> None:
    s = _settings(cookie_secure="auto", public_base_url=None, trusted_proxies="10.0.0.0/24")
    assert cookies.cookie_secure(_request({"X-Forwarded-Proto": "https"}, peer="10.0.0.2"), s) is True
    assert cookies.cookie_secure(_request({"X-Forwarded-Proto": "http"}, peer="10.0.0.2"), s) is False
    assert cookies.cookie_secure(_request({"X-Forwarded-Proto": "https"}, peer="9.9.9.9"), s) is False  # spoofable: ignored
    no_proxy = _settings(cookie_secure="auto", public_base_url=None, trusted_proxies="")
    assert cookies.cookie_secure(_request({"X-Forwarded-Proto": "https"}, peer="10.0.0.2"), no_proxy) is False


def test_cookie_secure_auto_uses_an_https_public_base_url() -> None:
    s = _settings(cookie_secure="auto", public_base_url="https://sales.example.com", trusted_proxies="")
    assert cookies.cookie_secure(_request(), s) is True


def test_every_cookie_helper_applies_the_decision() -> None:
    from sales0.web import acting

    resp = Response()
    cookies.set_cookie(resp, "k", "v", settings=_settings(cookie_secure="true"))
    assert "; secure" in resp.headers["set-cookie"].lower()
    resp = Response()
    cookies.delete_cookie(resp, "k", settings=_settings(cookie_secure="true"))
    assert "; secure" in resp.headers["set-cookie"].lower()
    token = cookies._bound.set(True)  # what the request middleware binds for an https request
    try:
        resp = Response()
        acting.set_flash(resp, "hello")
        acting.set_acting_cookie(resp, 1)
        assert all("; secure" in c.lower() for c in resp.headers.getlist("set-cookie")) and len(resp.headers.getlist("set-cookie")) == 2
    finally:
        cookies._bound.reset(token)


# ---------------------------------------------------------------------- #6 log redaction
def test_set_password_token_is_redacted_in_logged_paths() -> None:
    assert redact_path("/set-password/abc_DEF-123") == "/set-password/<redacted>"
    assert redact_path("/inbox") == "/inbox"


# ---------------------------------------------------------------------- #7 storage modes
def test_harden_storage_sets_owner_only_modes(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    for name in ("s.db", "s.db-wal", "s.db-shm", "secret.key"):
        (data / name).write_text("x")
        (data / name).chmod(0o644)
    data.chmod(0o755)
    harden_storage(_settings(data_dir=str(data), database_url=f"sqlite:///{data / 's.db'}"))
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    for name in ("s.db", "s.db-wal", "s.db-shm", "secret.key"):
        assert stat.S_IMODE((data / name).stat().st_mode) == 0o600


def test_harden_storage_ignores_missing_files(tmp_path: Path) -> None:
    harden_storage(_settings(data_dir=str(tmp_path / "nope"), database_url=f"sqlite:///{tmp_path / 'nope' / 'x.db'}"))


# ---------------------------------------------------------------------- config risks
def test_fixture_linkedin_mode_is_refused_on_a_public_host() -> None:
    s = _settings(linkedin_mode="fixture", host="0.0.0.0", anthropic_api_key=SecretStr("k"), app_password=SecretStr("pw"))
    with pytest.raises(StartupError, match="fixture"):
        check_startup(s)
    check_startup(_settings(linkedin_mode="fixture", host="127.0.0.1", anthropic_api_key=SecretStr("k")))


# ---------------------------------------------------------------------- #1 limits, #13 headers
def test_body_limits_only_widen_for_upload_paths() -> None:
    assert body_limit("POST", "/keywords")[0] == DEFAULT_BODY_LIMIT == 1024 * 1024
    assert body_limit("POST", "/import/csv")[0] > 5 * 1024 * 1024
    assert body_limit("POST", "/outreach/decks")[0] > get_settings().deck_max_bytes
    assert body_limit("GET", "/import/csv")[0] == DEFAULT_BODY_LIMIT


def test_csp_has_no_unsafe_inline_for_scripts_and_locks_framing() -> None:
    csp = dict(security_headers())[b"content-security-policy"].decode()
    script_src = next(d for d in csp.split("; ") if d.startswith("script-src"))
    assert script_src == "script-src 'self'"
    for directive in ("frame-ancestors 'none'", "base-uri 'self'", "object-src 'none'", "default-src 'self'"):
        assert directive in csp
    assert "accounts.google.com" in csp  # form-action: Connect Gmail redirects a POST to Google
