"""Typed application settings (prefix ``SALES0_``), loaded from the process env and ``.env``.

The ``.env`` file is resolved from the absolute repo root, so the app reads the same file
whatever the current working directory is. Secrets are ``SecretStr`` and are never logged;
use ``Settings.api_key_present`` when a log line needs to mention the key.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
ENV_FILE: Path = REPO_ROOT / ".env"

LinkedInMode = Literal["live", "fixture"]
LogFormat = Literal["json", "console"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
DateFilter = Literal["past-24h", "past-week", "past-month"]
BrowserChannel = Literal["auto", "chrome", "chromium"]
ScrapeSort = Literal["latest", "relevance"]
DATE_FILTER_VALUES: tuple[str, ...] = ("past-24h", "past-week", "past-month")

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
    # claude-haiku-5-5 pricing for prompts up to 100K tokens (USD per 1M tokens).
    llm_price_input_usd_per_mtok: float = Field(0.10, ge=0)
    llm_price_output_usd_per_mtok: float = Field(0.50, ge=0)

    # --- Server ---
    host: str = "127.0.0.1"
    port: int = Field(8765, ge=1, le=65535)

    # --- Storage ---
    data_dir: str = "data"
    database_url: str = "sqlite:///data/sales0.db"
    test_database_url: str | None = None

    # --- Google / Gmail (Phase 3D) ---
    google_client_secret_path: str = "data/google_client_secret.json"
    google_token_path: str = "data/google_token.json"

    # --- Team / outreach (Phase 3) ---
    # Encrypts Gmail tokens at rest and signs the "acting as" cookie. Any string (>= 16 chars)
    # or a Fernet key. Unset in dev: a key is generated once and kept at <data_dir>/secret.key.
    secret_key: SecretStr | None = None
    admin_email: str = "admin@smalltech.in"
    admin_name: str = "Admin"
    gmail_daily_send_cap: int = Field(40, ge=0)  # 0 = sending disabled
    deck_max_bytes: int = Field(10 * 1024 * 1024, gt=0)  # max size of one uploaded deck PDF
    # Only for a non-loopback deployment (e.g. https://sales.example.com); locally the OAuth
    # redirect URI is built from the request's own loopback host and port.
    public_base_url: str | None = None
    # Google endpoints; overridable so tests (and proxies) can point the OAuth flow elsewhere.
    google_auth_endpoint: str = "https://accounts.google.com/o/oauth2/v2/auth"
    google_token_endpoint: str = "https://oauth2.googleapis.com/token"
    google_userinfo_endpoint: str = "https://openidconnect.googleapis.com/v1/userinfo"
    google_revoke_endpoint: str = "https://oauth2.googleapis.com/revoke"

    # TEST ONLY: comma-separated hosts (or 1/true = loopback) the research fetcher may reach even
    # though they are private. Ignored unless linkedin_mode == "fixture" (see ``research_private_hosts``).
    research_allow_private_hosts: str = ""

    # --- Time ---
    timezone: str = "Asia/Kolkata"

    # --- Logging ---
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "json"
    log_file: str | None = None

    # --- LinkedIn ---
    linkedin_mode: LinkedInMode = "live"
    # Phase 1.1: auto = installed Google Chrome, else the bundled Chromium (with a Setup note);
    # chrome = Google Chrome only; chromium = bundled Chromium only (tests). Connect is always
    # headed and every check/run headless, so there is no headless setting any more.
    linkedin_browser_channel: BrowserChannel = "auto"
    linkedin_connect_timeout_s: int = Field(300, ge=10)
    # Phase 1.1: minutes Run now stays refused after a safety stop (0 = off).
    safety_cooldown_min: int = Field(60, ge=0)  # after rate_limited / captcha
    safety_cooldown_checkpoint_min: int = Field(15, ge=0)  # after checkpoint / logout

    # --- Scrape caps and pacing ---
    scrape_max_keywords_per_run: int = Field(40, ge=1)
    scrape_max_posts_per_keyword: int = Field(80, ge=1)
    scrape_max_posts_per_day: int = Field(2000, ge=1)
    scrape_max_runs_per_day: int = Field(4, ge=1)
    scrape_delay_min_s: float = Field(3.0, ge=0)
    scrape_delay_max_s: float = Field(9.0, ge=0)
    scrape_keyword_pause_min_s: float = Field(20.0, ge=0)
    scrape_keyword_pause_max_s: float = Field(60.0, ge=0)
    scrape_max_scrolls: int = Field(20, ge=0)
    # Phase 1.4: consecutive scrolls without new cards that end a search pass.
    scrape_no_growth_scrolls: int = Field(2, ge=1)
    # Phase 1.4: latest = LinkedIn sortBy="date_posted"; relevance = LinkedIn's default ranking.
    scrape_sort: ScrapeSort = "latest"
    # Phase 1.4: one search pass per date filter, in order (comma list in the env).
    scrape_date_filters: Annotated[tuple[str, ...], NoDecode] = (
        "past-week",
        "past-month",
    )
    # Legacy single filter (Phase 1..1.3). Read only as a one-item list when ..._FILTERS is unset.
    scrape_date_filter_legacy: str | None = Field(
        None, validation_alias="SALES0_SCRAPE_DATE_FILTER"
    )
    # Phase 1.5: per-phrase search variants (other sort, quoted, hashtag, urgent/requirement/enquiry
    # suffixes, other date filters) run as extra passes in a daily rotation; capped per phrase.
    scrape_variants: bool = True
    scrape_max_passes_per_keyword: int = Field(6, ge=1, le=12)
    # Phase 1.4: max "see more" clicks per keyword (truncated cards only).
    scrape_expand_max_per_keyword: int = Field(40, ge=0)
    # Phase 1.2: max "Copy link to post" attempts per keyword (posts without a URN); 0 = off.
    scrape_permalink_max_per_keyword: int = Field(15, ge=0)

    # --- Triage ---
    fewshot_max_examples: int = Field(6, ge=0)
    fewshot_text_chars: int = Field(600, ge=50)
    triage_max_input_chars: int = Field(4000, ge=200)
    # Phase 1.4
    triage_concurrency: int = Field(4, ge=1, le=8)
    seen_raw_retention_days: int = Field(14, ge=0)
    seed_suggested_keywords: bool = True

    # --- Reply tracking (read-only polling of each member's own Gmail) ---
    reply_poll_minutes: int = Field(10, ge=0)  # 0 disables the background poller
    reply_lookback_days: int = Field(60, ge=1)  # only sent messages younger than this are polled
    reply_max_threads_per_poll: int = Field(300, ge=1)  # per member per cycle
    # Extra addresses that are "us" (Send-As aliases), comma separated; never counted as replies.
    reply_own_aliases: str = ""

    # --- Google Sheet sync (read-only; Phase 2) ---
    sheet_sync_minutes: int = Field(30, ge=0)  # 0 disables the interval scheduler
    sheet_client_sourcing_id: str = "1xUdfMXUKFRNd3RXTq-PAynlbFqyI5sfWUtEHDtlw3ag"
    sheet_metrics_id: str = "1tnwiBRMfp_QR0CmAkxZM_Chl-VAixmcWPxE1NCGED5w"
    # Optional JSON list replacing the default tabs: [{"name","kind","gid","sheet_id"?}, ...]
    sheet_tabs_json: str | None = None
    sheet_fetch_timeout_s: float = Field(20.0, gt=0)
    sheet_fetch_retries: int = Field(3, ge=0, le=10)
    sheet_fetch_max_bytes: int = Field(20_000_000, ge=1000)

    # --- Ops (Phase 4): spend cap, scheduler, digest, purge ---
    # Monthly AI spend cap in USD (local calendar month). 0 = no cap. Over the cap every LLM
    # feature stops (there is no "essential" call): runs keep their enquiries pending.
    monthly_spend_cap_usd: float = Field(0.0, ge=0)
    spend_cap_warn_pct: int = Field(80, ge=1, le=100)
    scheduler_enabled: bool = True  # False = no background thread (pages still work)
    scheduler_poll_s: float = Field(5.0, gt=0)
    # Nightly discovery window in local time, "HH:MM"; the start time is jittered inside it.
    scheduler_window_start: str = "01:00"
    scheduler_window_end: str = "05:00"
    scheduler_discovery_max_runs_per_day: int = Field(1, ge=1)  # also bounded by scrape_max_runs_per_day
    scheduler_digest_time: str = "07:30"  # local time the daily digest is built
    scheduler_purge_time: str = "04:15"
    scheduler_watch_time: str = "05:30"  # profile_watch job (default disabled in the scheduler)

    # --- Prospect watch ---
    watch_max_profiles_per_run: int = Field(25, ge=1)
    watch_min_days_between_checks: float = Field(3.0, ge=0)
    watch_max_posts_per_profile: int = Field(15, ge=1)
    watch_profile_pause_min_s: float = Field(20.0, ge=0)
    watch_profile_pause_max_s: float = Field(60.0, ge=0)
    watch_append_to_discovery: bool = False  # also visit due profiles at the end of each discovery run
    purge_retention_days: int = Field(90, ge=7)
    digest_ai_summary: bool = True  # one Haiku call (feature "suggest") for the 5-line summary

    # ------------------------------------------------------------------ validators
    @field_validator(
        "linkedin_mode",
        "linkedin_browser_channel",
        "log_format",
        "scrape_sort",
        mode="before",
    )
    @classmethod
    def _strip_enum_lower(cls, value: Any) -> Any:
        value = _clean_enum_value(value)
        return value.lower() if isinstance(value, str) else value

    @field_validator("scrape_date_filters", mode="before")
    @classmethod
    def _parse_date_filters(cls, value: Any) -> Any:
        if isinstance(value, str) and not _clean_enum_value(value):
            return ("past-week", "past-month")  # blank = the default
        if isinstance(value, str):
            value = [part for part in _clean_enum_value(value).split(",")]
        if isinstance(value, (list, tuple)):
            items = tuple(str(v).strip().lower() for v in value if str(v).strip())
            if not items:
                raise ValueError(
                    "SALES0_SCRAPE_DATE_FILTERS needs at least one of "
                    + ", ".join(DATE_FILTER_VALUES)
                )
            bad = [v for v in items if v not in DATE_FILTER_VALUES]
            if bad:
                raise ValueError(
                    f"Unknown date filter {bad[0]!r}; use {', '.join(DATE_FILTER_VALUES)}"
                )
            return items
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _strip_enum_upper(cls, value: Any) -> Any:
        value = _clean_enum_value(value)
        return value.upper() if isinstance(value, str) else value

    @field_validator(
        "llm_model", "host", "timezone", "anthropic_workspace_id", mode="before"
    )
    @classmethod
    def _strip_plain(cls, value: Any) -> Any:
        return _clean_enum_value(value)

    @field_validator("admin_email", mode="before")
    @classmethod
    def _lower_admin_email(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("admin_name", "public_base_url", mode="before")
    @classmethod
    def _strip_team_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("google_client_secret_path", "google_token_path", mode="before")
    @classmethod
    def _strip_google_paths(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("sheet_client_sourcing_id", "sheet_metrics_id", "sheet_tabs_json", mode="before")
    @classmethod
    def _strip_sheet(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

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
            raise ValueError(
                f"Unknown timezone '{value}' — use an IANA name like Asia/Kolkata"
            ) from exc
        return value

    @field_validator("scheduler_window_start", "scheduler_window_end", "scheduler_digest_time", "scheduler_purge_time", "scheduler_watch_time", mode="before")
    @classmethod
    def _valid_hhmm(cls, value: Any) -> Any:
        value = _clean_enum_value(value)
        if isinstance(value, str):
            parts = value.split(":")
            if len(parts) != 2 or not all(p.isdigit() for p in parts) or not (0 <= int(parts[0]) < 24 and 0 <= int(parts[1]) < 60):
                raise ValueError("use a 24-hour HH:MM time like 01:00")
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
        return value

    @model_validator(mode="after")
    def _ranges(self) -> Settings:
        if self.scheduler_window_start >= self.scheduler_window_end:
            raise ValueError("SALES0_SCHEDULER_WINDOW_START must be earlier than SALES0_SCHEDULER_WINDOW_END (same day)")
        if self.scrape_delay_min_s > self.scrape_delay_max_s:
            raise ValueError(
                "SALES0_SCRAPE_DELAY_MIN_S must be <= SALES0_SCRAPE_DELAY_MAX_S"
            )
        if self.scrape_keyword_pause_min_s > self.scrape_keyword_pause_max_s:
            raise ValueError(
                "SALES0_SCRAPE_KEYWORD_PAUSE_MIN_S must be <= SALES0_SCRAPE_KEYWORD_PAUSE_MAX_S"
            )
        legacy = self.scrape_date_filter_legacy
        if legacy and "scrape_date_filters" not in self.model_fields_set:
            legacy = _clean_enum_value(legacy).lower()
            if legacy not in DATE_FILTER_VALUES:
                raise ValueError(
                    f"Unknown SALES0_SCRAPE_DATE_FILTER {legacy!r}; use {', '.join(DATE_FILTER_VALUES)}"
                )
            object.__setattr__(self, "scrape_date_filters", (legacy,))
        return self

    # ------------------------------------------------------------------ derived values
    @property
    def scrape_date_filter(self) -> str:
        """Read-only: the first configured date filter (Phase 1..1.3 callers)."""
        return self.scrape_date_filters[0]

    @property
    def api_key_present(self) -> bool:
        return bool(
            self.anthropic_api_key and self.anthropic_api_key.get_secret_value().strip()
        )

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def data_path(self) -> Path:
        """Absolute runtime dir; relative values resolve against the repo root."""
        path = Path(self.data_dir).expanduser()
        return path if path.is_absolute() else REPO_ROOT / path

    @property
    def research_private_hosts(self) -> list[str]:
        """Hosts the research fetcher may reach although private. Always [] outside fixture mode."""
        raw = self.research_allow_private_hosts.strip()
        if self.linkedin_mode != "fixture" or not raw:
            return []
        if raw.lower() in {"1", "true", "yes", "on"}:
            return ["127.0.0.1", "localhost"]
        return [h.strip().lower() for h in raw.split(",") if h.strip()]

    @property
    def google_client_secret_file(self) -> Path:
        path = Path(self.google_client_secret_path).expanduser()
        return path if path.is_absolute() else REPO_ROOT / path

    @property
    def google_token_file(self) -> Path:
        path = Path(self.google_token_path).expanduser()
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
        raise StartupError(
            "Set SALES0_APP_PASSWORD (Phase 2) before binding to a public host"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
