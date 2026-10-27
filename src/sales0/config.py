"""Typed application settings (prefix ``SALES0_``), loaded from the process env and ``.env``.

The ``.env`` file is resolved from the absolute repo root, so the app reads the same file
whatever the current working directory is. Secrets are ``SecretStr`` and are never logged;
use ``Settings.api_key_present`` when a log line needs to mention the key.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
ENV_FILE: Path = REPO_ROOT / ".env"

LinkedInMode = Literal["live", "fixture"]
LogFormat = Literal["json", "console"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
DateFilter = Literal["past-24h", "past-week", "past-month"]

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _clean_enum_value(value: Any) -> Any:
    """Strip an inline ``# comment`` and surrounding whitespace from an env value."""
    if isinstance(value, str):
        return value.split("#", 1)[0].strip()
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SALES0_",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    # --- LLM ---
    anthropic_api_key: SecretStr | None = None
    # Only needed when the key is NOT scoped to a workspace (Anthropic then requires the
    # anthropic-workspace-id header). Not in the original spec table; see .env.example.
    anthropic_workspace_id: str | None = None
    llm_model: str = "claude-haiku-5-5"
    llm_max_output_tokens: int = Field(900, ge=1, le=8192)
    llm_timeout_s: float = Field(30.0, gt=0)
    llm_max_retries: int = Field(3, ge=0, le=10)
    llm_price_input_usd_per_mtok: float = Field(1.00, ge=0)
    llm_price_output_usd_per_mtok: float = Field(5.00, ge=0)

    # --- Server ---
    host: str = "127.0.0.1"
    port: int = Field(8765, ge=1, le=65535)

    # --- Storage ---
    data_dir: str = "data"
    database_url: str = "sqlite:///data/sales0.db"
    test_database_url: str | None = None

    # --- Time ---
    timezone: str = "Asia/Kolkata"

    # --- Logging ---
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "json"
    log_file: str | None = None

    # --- LinkedIn ---
    linkedin_mode: LinkedInMode = "live"
    linkedin_headless: bool = False
    linkedin_connect_timeout_s: int = Field(300, ge=10)

    # --- Scrape caps and pacing ---
    scrape_max_keywords_per_run: int = Field(10, ge=1)
    scrape_max_posts_per_keyword: int = Field(15, ge=1)
    scrape_max_posts_per_day: int = Field(150, ge=1)
    scrape_max_runs_per_day: int = Field(3, ge=1)
    scrape_delay_min_s: float = Field(3.0, ge=0)
    scrape_delay_max_s: float = Field(9.0, ge=0)
    scrape_keyword_pause_min_s: float = Field(20.0, ge=0)
    scrape_keyword_pause_max_s: float = Field(60.0, ge=0)
    scrape_max_scrolls: int = Field(4, ge=0)
    scrape_date_filter: DateFilter = "past-week"

    # --- Triage ---
    fewshot_max_examples: int = Field(6, ge=0)
    fewshot_text_chars: int = Field(600, ge=50)
    triage_max_input_chars: int = Field(4000, ge=200)

    # ------------------------------------------------------------------ validators
    @field_validator("linkedin_mode", "log_format", "scrape_date_filter", mode="before")
    @classmethod
    def _strip_enum_lower(cls, value: Any) -> Any:
        value = _clean_enum_value(value)
        return value.lower() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _strip_enum_upper(cls, value: Any) -> Any:
        value = _clean_enum_value(value)
        return value.upper() if isinstance(value, str) else value

    @field_validator("llm_model", "host", "timezone", "anthropic_workspace_id", mode="before")
    @classmethod
    def _strip_plain(cls, value: Any) -> Any:
        return _clean_enum_value(value)

    @field_validator("data_dir", "database_url", "test_database_url", "log_file", mode="before")
    @classmethod
    def _strip_whitespace(cls, value: Any) -> Any:
        # No comment stripping here: URLs and paths may legitimately contain '#'.
        return value.strip() if isinstance(value, str) else value

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown timezone '{value}' — use an IANA name like Asia/Kolkata") from exc
        return value

    @model_validator(mode="after")
    def _ranges(self) -> Settings:
        if self.scrape_delay_min_s > self.scrape_delay_max_s:
            raise ValueError("SALES0_SCRAPE_DELAY_MIN_S must be <= SALES0_SCRAPE_DELAY_MAX_S")
        if self.scrape_keyword_pause_min_s > self.scrape_keyword_pause_max_s:
            raise ValueError("SALES0_SCRAPE_KEYWORD_PAUSE_MIN_S must be <= SALES0_SCRAPE_KEYWORD_PAUSE_MAX_S")
        return self

    # ------------------------------------------------------------------ derived values
    @property
    def api_key_present(self) -> bool:
        return bool(self.anthropic_api_key and self.anthropic_api_key.get_secret_value().strip())

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def data_path(self) -> Path:
        """Absolute runtime dir; relative values resolve against the repo root."""
        path = Path(self.data_dir).expanduser()
        return path if path.is_absolute() else REPO_ROOT / path

    @property
    def log_file_path(self) -> Path | None:
        if not self.log_file:
            return None
        path = Path(self.log_file).expanduser()
        return path if path.is_absolute() else REPO_ROOT / path

    @property
    def resolved_database_url(self) -> str:
        """The DB URL with relative SQLite file paths anchored at the repo root."""
        url = self.database_url
        prefix = "sqlite:///"
        if url.startswith(prefix) and not url.startswith("sqlite:////"):
            rest = url[len(prefix) :]
            if rest and rest != ":memory:" and not rest.startswith(":"):
                return f"{prefix}{(REPO_ROOT / rest).as_posix()}"
        return url

    @property
    def is_loopback_host(self) -> bool:
        if self.host in LOOPBACK_HOSTS:
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class StartupError(RuntimeError):
    """A configuration problem that must stop the server before it binds."""


def check_startup(settings: Settings) -> None:
    """Fail loudly on configuration that must never reach a running server."""
    if not settings.api_key_present:
        raise StartupError(
            "SALES0_ANTHROPIC_API_KEY is missing — add it to .env in the repo root (see .env.example)."
        )
    if not settings.is_loopback_host:
        raise StartupError("Set SALES0_APP_PASSWORD (Phase 2) before binding to a public host")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
