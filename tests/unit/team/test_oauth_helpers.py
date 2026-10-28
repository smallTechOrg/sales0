"""PKCE, the single-use state store and redirect-URI building."""

from __future__ import annotations

import base64
import hashlib
from datetime import timedelta

import pytest

from sales0.clock import utcnow
from sales0.config import reset_settings
from sales0.errors import DomainError
from sales0.services.team import oauth


def test_pkce_challenge_is_s256_of_the_verifier() -> None:
    verifier = oauth.new_code_verifier()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert oauth.code_challenge(verifier) == expected
    assert "=" not in oauth.code_challenge(verifier)


def test_state_is_random_single_use_and_bound_to_the_member() -> None:
    store = oauth.StateStore()
    a = store.create(1, "http://127.0.0.1:1/x")
    b = store.create(1, "http://127.0.0.1:1/x")
    assert a.state != b.state and a.verifier != b.verifier
    assert store.consume(a.state, 1).verifier == a.verifier
    with pytest.raises(DomainError) as reuse:
        store.consume(a.state, 1)
    assert reuse.value.code == "oauth_state_invalid"
    with pytest.raises(DomainError) as other:
        store.consume(b.state, 2)
    assert other.value.code == "oauth_state_member"
    with pytest.raises(DomainError):  # burned even though it was rejected
        store.consume(b.state, 1)


def test_expired_and_unknown_state_are_rejected() -> None:
    store = oauth.StateStore()
    p = store.create(1, "u", now=utcnow() - timedelta(minutes=11))
    with pytest.raises(DomainError) as exc:
        store.consume(p.state, 1)
    assert exc.value.code == "oauth_state_expired"
    for bad in (None, "", "nope"):
        with pytest.raises(DomainError):
            store.consume(bad, 1)


@pytest.mark.parametrize(
    ("scheme", "host", "port", "expected"),
    [
        ("http", "127.0.0.1", 8766, "http://127.0.0.1:8766/oauth/google/callback"),
        ("http", "localhost", 9000, "http://localhost:9000/oauth/google/callback"),
        ("http", "[::1]", 8766, "http://[::1]:8766/oauth/google/callback"),
        ("http", "127.0.0.1", None, "http://127.0.0.1/oauth/google/callback"),
    ],
)
def test_redirect_uri_follows_the_loopback_origin(scheme: str, host: str, port: int | None, expected: str) -> None:
    assert oauth.redirect_uri_for(scheme, host, port) == expected


def test_redirect_uri_from_request_uses_the_host_header() -> None:
    assert (
        oauth.redirect_uri_for_request("http://whatever/team", "127.0.0.1:8766")
        == "http://127.0.0.1:8766/oauth/google/callback"
    )


@pytest.mark.parametrize("host", ["example.com", "192.168.1.5", "sales.local", "testserver"])
def test_non_loopback_hosts_are_refused_with_a_clear_message(host: str) -> None:
    with pytest.raises(DomainError) as exc:
        oauth.redirect_uri_for("http", host, 8766)
    assert exc.value.code == "oauth_host_not_allowed" and "localhost or 127.0.0.1" in exc.value.message


def test_public_base_url_overrides_the_loopback_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_PUBLIC_BASE_URL", "https://sales.example.com/")
    reset_settings()
    assert oauth.redirect_uri_for("http", "anything", 1) == "https://sales.example.com/oauth/google/callback"
