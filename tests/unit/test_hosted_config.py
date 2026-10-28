"""SALES0_MODE=hosted: settings, URL building, and the boot-time refusals (no open mode, no SQLite, no missing secret)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from sales0.config import Settings, StartupError, check_startup

KEY = SecretStr("not-a-real-key")
GOOD: dict[str, Any] = {
    "mode": "hosted",
    "anthropic_api_key": KEY,
    "database_url": "postgresql://u:p@db.internal:5432/sales0",
    "secret_key": "k" * 32,
    "public_base_url": "https://sales0-1.us-central1.run.app",
    "worker_token": "w" * 40,
    "app_password": "bootstrap-password",
    "host": "0.0.0.0",
}


def make(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **{**GOOD, **overrides})


def test_a_complete_hosted_configuration_boots() -> None:
    check_startup(make())


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        ({"database_url": "sqlite:///x.db"}, "PostgreSQL"),
        ({"secret_key": None}, "SALES0_SECRET_KEY"),
        ({"public_base_url": "http://example.test"}, "https"),
        ({"public_base_url": None}, "https"),
        ({"worker_token": None}, "SALES0_WORKER_TOKEN"),
        ({"app_password": None}, "open mode"),
        ({"linkedin_mode": "fixture"}, "fixture"),
    ],
)
def test_hosted_mode_refuses_to_boot_half_configured(override: dict[str, Any], needle: str) -> None:
    with pytest.raises(StartupError, match=needle):
        check_startup(make(**override))


def test_hosted_mode_has_no_background_threads_by_construction() -> None:
    s = make(scheduler_enabled=True, reply_poll_minutes=10, sheet_sync_minutes=30)
    assert (s.scheduler_enabled, s.reply_poll_minutes, s.sheet_sync_minutes) == (False, 0, 0)
    local = Settings(_env_file=None, anthropic_api_key=KEY, scheduler_enabled=True, reply_poll_minutes=10)
    assert local.scheduler_enabled and local.reply_poll_minutes == 10 and not local.is_hosted


def test_the_worker_token_must_be_long() -> None:
    with pytest.raises(ValidationError, match="at least 32"):
        make(worker_token="short")
    assert make(worker_token="  ").worker_token is None


def test_bare_postgres_schemes_use_the_psycopg_driver() -> None:
    assert make(database_url="postgres://u:p@h/db").resolved_database_url == "postgresql+psycopg://u:p@h/db"
    assert make(database_url="postgresql://u:p@h/db").resolved_database_url == "postgresql+psycopg://u:p@h/db"
    assert make(database_url="postgresql+psycopg://u:p@h/db").resolved_database_url == "postgresql+psycopg://u:p@h/db"
    assert not make().is_sqlite


def test_db_parts_build_the_url_and_escape_the_password() -> None:
    s = make(db_host="10.1.2.3", db_password=SecretStr("p@ss/w:rd#1"), database_url="sqlite:///ignored.db")
    url = s.resolved_database_url
    assert url.startswith("postgresql+psycopg://sales0_app:") and "@10.1.2.3:5432/sales0" in url
    assert "p@ss/w:rd#1" not in url  # percent-escaped, never raw
    assert "sslmode=prefer" in url
    from sqlalchemy.engine import make_url

    assert make_url(url).password == "p@ss/w:rd#1"
    assert "p@ss" not in repr(s)  # SecretStr


def test_local_defaults_are_unchanged() -> None:
    s = Settings(_env_file=None, anthropic_api_key=KEY)
    assert s.mode == "local" and s.worker_token is None and not s.auto_migrate
    assert s.resolved_database_url.startswith("sqlite:///") and s.is_sqlite
