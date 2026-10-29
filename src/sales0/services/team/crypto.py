"""Secret handling for team features: Fernet encryption of Gmail tokens at rest and HMAC
signing of the "acting as" / flash cookies, both derived from ``SALES0_SECRET_KEY``.

If the key is unset (dev), one is generated and persisted at ``<data_dir>/secret.key``
(mode 0600) with a loud warning. Tokens and keys are never logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

from sales0.config import Settings, get_settings
from sales0.logging import get_logger

log = get_logger("sales0.team.crypto")

KEY_FILE_NAME = "secret.key"


class DecryptionError(Exception):
    """The ciphertext is corrupt or was encrypted with a different secret key."""


def master_secret(settings: Settings | None = None) -> bytes:
    """The raw secret: SALES0_SECRET_KEY, else the persisted dev key (generated once)."""
    settings = settings or get_settings()
    if settings.secret_key is not None:
        value = settings.secret_key.get_secret_value().strip()
        if value:
            return value.encode("utf-8")
    path = settings.data_path / KEY_FILE_NAME
    if path.is_file():
        stored = path.read_text("utf-8").strip()
        if stored:
            return stored.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    generated = Fernet.generate_key().decode("ascii")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(generated)
    os.chmod(path, 0o600)
    log.warning(
        "secret_key_generated_for_dev",
        message=(
            "!!! SALES0_SECRET_KEY is not set: generated a development key and saved it to "
            f"{path}. Set SALES0_SECRET_KEY in .env for any real deployment; if this file is lost, "
            "every member must reconnect Gmail."
        ),
    )
    return generated.encode("ascii")


def _derive(secret: bytes, purpose: bytes) -> bytes:
    return hmac.new(secret, b"sales0:" + purpose, hashlib.sha256).digest()


def fernet(settings: Settings | None = None) -> Fernet:
    secret = master_secret(settings)
    return Fernet(base64.urlsafe_b64encode(_derive(secret, b"fernet")))


def encrypt(plaintext: str, settings: Settings | None = None) -> str:
    return fernet(settings).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str, settings: Settings | None = None) -> str:
    try:
        return fernet(settings).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise DecryptionError("The stored Gmail token cannot be decrypted (wrong or changed SALES0_SECRET_KEY).") from exc


def sign(value: str, purpose: str = "cookie", settings: Settings | None = None) -> str:
    """``value.signature`` (url-safe). ``value`` must not need escaping (ids, tokens)."""
    mac = hmac.new(_derive(master_secret(settings), purpose.encode()), value.encode("utf-8"), hashlib.sha256)
    return f"{value}.{base64.urlsafe_b64encode(mac.digest()).decode('ascii').rstrip('=')}"


def unsign(signed: str | None, purpose: str = "cookie", settings: Settings | None = None) -> str | None:
    """The original value, or None when missing/tampered."""
    if not signed or "." not in signed:
        return None
    value, _, _sig = signed.rpartition(".")
    expected = sign(value, purpose, settings)
    return value if hmac.compare_digest(expected, signed) else None


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)
