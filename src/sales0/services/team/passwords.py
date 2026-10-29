"""Password hashing (argon2id) and the new-password policy."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from sales0.config import Settings, get_settings
from sales0.errors import DomainError

_hasher = PasswordHasher()  # argon2id with the library's RFC 9106 low-memory defaults
# Verified when the email is unknown, so "no such user" costs the same as "wrong password".
_DUMMY_HASH = _hasher.hash("sales0-dummy-password-for-timing")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str | None, candidate: str) -> bool:
    """Constant-effort verify; a missing hash still burns one argon2 verification."""
    try:
        _hasher.verify(stored or _DUMMY_HASH, candidate)
    except (VerificationError, InvalidHashError):
        return False
    return stored is not None


def needs_rehash(stored: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored)
    except InvalidHashError:
        return False


def validate_new_password(password: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if len(password) < settings.password_min_length:
        raise DomainError(
            "password_weak", f"Use at least {settings.password_min_length} characters for the password.", 400
        )
    if len(password) > 256:
        raise DomainError("password_weak", "That password is too long (256 characters at most).", 400)
    if not password.strip():
        raise DomainError("password_weak", "The password cannot be blank.", 400)
    return password
