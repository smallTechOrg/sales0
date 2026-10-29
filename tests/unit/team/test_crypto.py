"""Fernet token encryption, HMAC signing and the dev key file."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from sales0.config import get_settings, reset_settings
from sales0.services.team import crypto


def _set_key(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("SALES0_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("SALES0_SECRET_KEY", value)
    reset_settings()


def test_encrypt_roundtrip_and_ciphertext_hides_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, "k" * 32)
    token = crypto.encrypt("1//refresh-token-value")
    assert "refresh-token-value" not in token
    assert crypto.decrypt(token) == "1//refresh-token-value"


def test_wrong_key_fails_to_decrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, "a" * 32)
    token = crypto.encrypt("secret")
    _set_key(monkeypatch, "b" * 32)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(token)


def test_garbage_ciphertext_is_a_decryption_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, "k" * 32)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt("not-a-fernet-token")


def test_sign_unsign_and_tamper_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, "k" * 32)
    signed = crypto.sign("42", "acting")
    assert crypto.unsign(signed, "acting") == "42"
    assert crypto.unsign(signed.replace("42", "43", 1), "acting") is None
    assert crypto.unsign(signed, "flash") is None  # purposes are separated
    assert crypto.unsign(None) is None and crypto.unsign("nodot") is None


def test_unset_key_generates_once_and_persists_mode_600(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, None)
    path = get_settings().data_path / crypto.KEY_FILE_NAME
    assert not path.exists()
    token = crypto.encrypt("hello")
    assert path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600
    first = path.read_text()
    assert crypto.decrypt(token) == "hello"  # second call reuses the same file
    assert path.read_text() == first


def test_configured_key_never_writes_a_key_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, "k" * 32)
    crypto.encrypt("x")
    assert not Path(get_settings().data_path / crypto.KEY_FILE_NAME).exists()
