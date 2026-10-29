"""argon2id hashing and the password policy."""

from __future__ import annotations

import pytest

from sales0.config import get_settings
from sales0.errors import DomainError
from sales0.services.team import passwords


def test_hash_is_argon2id_salted_and_verifies() -> None:
    a, b = passwords.hash_password("correct horse battery"), passwords.hash_password("correct horse battery")
    assert a.startswith("$argon2id$") and a != b  # random salt
    assert passwords.verify_password(a, "correct horse battery")
    assert not passwords.verify_password(a, "correct horse batter")
    assert not passwords.verify_password(a, "")


def test_missing_or_garbage_hash_never_verifies() -> None:
    assert passwords.verify_password(None, "anything-at-all") is False
    assert passwords.verify_password("not-a-hash", "anything-at-all") is False
    assert passwords.needs_rehash("not-a-hash") is False


def test_policy_min_length_blank_and_max() -> None:
    s = get_settings()
    n = s.password_min_length
    assert passwords.validate_new_password("x" * n, s) == "x" * n
    for bad in ("x" * (n - 1), " " * n, "x" * 257):
        with pytest.raises(DomainError) as exc:
            passwords.validate_new_password(bad, s)
        assert exc.value.code == "password_weak" and exc.value.http_status == 400
