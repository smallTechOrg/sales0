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
CookieSecure = Literal["auto", "true", "false"]
AppMode = Literal["local", "hosted"]
LogFormat = Literal["json", "console"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
DateFilter = Literal["past-24h", "past-week", "past-month"]
BrowserChannel = Literal["auto", "chrome", "chromium"]
ScrapeSort = Literal["latest", "relevance"]
DATE_FILTER_VALUES: tuple[str, ...] = ("past-24h", "past-week", "past-month")

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MIN_WORKER_TOKEN_CHARS = 32
MIN_SECRET_KEY_CHARS = 16  # a Fernet key is 44 chars, so "16+ chars or a Fernet key" is just this floor


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

    # --- Deployment mode ---
    # local  = today's single-machine app (scheduler thread, local Chrome runs, SQLite by default).
    # hosted = Cloud Run + PostgreSQL: no scheduler / poller thread, no local-browser runs (a laptop
    #          `sales0 worker` scrapes), every manual action does its work inside the request.
    mode: AppMode = "local"
    # Bearer token the laptop worker presents to /api/worker/*. >= 32 chars (openssl rand -base64 36).
    worker_token: SecretStr | None = None
    # Run pending Alembic migrations (and create the database if it is missing) at boot, under a PostgreSQL
    # advisory lock. Meant for the hosted single instance; locally you run `alembic upgrade head` yourself.
    auto_migrate: bool = False
    # Hosted: a manual action stops starting new work after this many seconds and says "more remaining, click
    # again" (Cloud Run only gives CPU during the request; the request timeout is 900 s).
    hosted_action_budget_s: int = Field(240, ge=10, le=840)
    # Worker API: a run claimed by a worker that has not sent a heartbeat for this long is considered lost.
    worker_stale_s: int = Field(180, ge=30)

    # --- Laptop worker (`sales0 worker`) ---
    worker_server_url: str | None = None  # https://<cloud-run-url>; the worker polls <url>/api/worker/*
    worker_poll_s: float = Field(15.0, gt=0)  # idle poll interval
    worker_heartbeat_s: float = Field(30.0, gt=0)
    worker_upload_batch: int = Field(25, ge=1, le=200)  # raw posts per upload request
    worker_name: str | None = None  # label shown in the hosted UI (default: this machine's host name)

    # --- Server ---
    host: str = "127.0.0.1"
    port: int = Field(8765, ge=1, le=65535)

    # --- Storage ---
    data_dir: str = "data"
    # Fixscrape D: escape hatch for the one-process-per-data-dir lock (<data_dir>/.sales0.lock). Unsupported.
    allow_multi_process: bool = False
    database_url: str = "sqlite:///data/sales0.db"
    test_database_url: str | None = None
    # Hosted alternative to SALES0_DATABASE_URL: the parts of a PostgreSQL URL (the password comes from Secret
    # Manager as a plain env var, so it never needs URL-escaping). Used when SALES0_DB_HOST is set.
    db_host: str | None = None
    db_port: int = Field(5432, ge=1, le=65535)
    db_name: str = "sales0"
    db_user: str = "sales0_app"
    db_password: SecretStr | None = None
    db_sslmode: str = "prefer"  # Cloud SQL (ENCRYPTED_ONLY) accepts prefer/require
    # Small pools: the shared Cloud SQL instance is a db-f1-micro (~25 connections for every service).
    db_pool_size: int = Field(2, ge=1, le=20)
    db_max_overflow: int = Field(2, ge=0, le=20)
    db_pool_recycle_s: int = Field(300, ge=30)

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
    # Extra Host names this server answers to (comma separated, port-agnostic) once logins are enabled, on top of
    # loopback, SALES0_HOST and the host of SALES0_PUBLIC_BASE_URL. Any other Host header is refused (DNS rebinding).
    allowed_hosts: str = ""
    # Secure flag on every cookie: auto = when the request is https (or X-Forwarded-Proto=https from a trusted
    # proxy, or SALES0_PUBLIC_BASE_URL is https); true / false force it.
    cookie_secure: CookieSecure = "auto"
    # TEST ONLY: also accept the Starlette TestClient's default Host ("testserver"). Set by tests/conftest.py.
    allow_testserver_host: bool = False
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
    # Fixscrape D: conservative defaults. 2 runs x (40 keywords x 3 passes = 120 searches) = 240 of the
    # 600 page loads/day; one normal run still scans >= 200 posts (40 keywords x 3 passes x several cards).
    scrape_max_runs_per_day: int = Field(2, ge=1)
    # Search/profile page loads per LinkedIn account per local day (search passes, profile visits and
    # permalink pages all count; scanned pages count, not only kept posts). A run stops when it is used up.
    scrape_max_pageloads_per_day: int = Field(600, ge=1)
    # Consecutive page errors (timeouts / failed loads) that stop a run as 'rate_limited' (cool-down).
    scrape_max_consecutive_errors: int = Field(3, ge=1)
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
    scrape_max_passes_per_keyword: int = Field(3, ge=1, le=12)
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
    sheet_client_sourcing_id: str = ""  # opt-in: blank = that sheet is not synced
    sheet_metrics_id: str = ""  # opt-in: blank = that sheet is not synced
    # Optional JSON list replacing the default tabs: [{"name","kind","gid","sheet_id"?}, ...]
    sheet_tabs_json: str | None = None
    sheet_fetch_timeout_s: float = Field(20.0, gt=0)
    sheet_fetch_retries: int = Field(3, ge=0, le=10)
    sheet_fetch_max_bytes: int = Field(20_000_000, ge=1000)

    # --- CRM / shared login (Phase 2) ---
    # BOOTSTRAP only: the first admin's initial password while no admin has one. Open mode (no passwords at all)
    # is loopback only; check_startup refuses a public host without any auth.
    app_password: SecretStr | None = None
    session_days: int = Field(14, ge=1, le=90)
    login_max_attempts: int = Field(5, ge=1)
    login_lockout_s: int = Field(60, ge=1)
    # --- Per-user accounts (Phase 5) ---
    session_remember_days: int = Field(30, ge=1, le=365)  # "remember me" cookie lifetime
    trusted_proxies: str = ""  # comma-separated IPs/CIDRs whose X-Forwarded-For may be trusted
    password_reset_ttl_h: int = Field(24, ge=1, le=168)  # lifetime of an admin-issued set-password link
    password_min_length: int = Field(10, ge=8, le=128)
    # --- LinkedIn accounts (Phase 5) ---
    linkedin_session_backend: Literal["folder", "db"] = "folder"  # backend for NEW accounts (default account is folder)
    followup_days_1: int = Field(3, ge=1)
    followup_days_2: int = Field(7, ge=1)
    followup_days_final: int = Field(14, ge=1)
    stale_lead_days: int = Field(14, ge=1)
    repeat_poster_min: int = Field(3, ge=2)
    repeat_poster_days: int = Field(60, ge=1)
    crm_page_size: int = Field(50, ge=5, le=500)

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
    # Fixscrape D: profile visits per LinkedIn account per local day (counted in the page-load total too)
    # and watch runs per account per day (watch runs do not use the discovery run allowance).
    watch_max_visits_per_day: int = Field(30, ge=1)
    watch_max_runs_per_day: int = Field(2, ge=1)
    # Scheduled watch start = scheduler_watch_time + a per-day pseudo-random 0..N minute delay (0 = exact time),
    # so the account is not opened at the same minute every day.
    watch_start_jitter_min: int = Field(30, ge=0, le=240)
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
        "cookie_secure",
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

    @field_validator("secret_key", mode="before")
    @classmethod
    def _secret_key_strong_enough(cls, value: Any) -> Any:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str):
            return value
        raw = raw.strip()
        if not raw:
            return None  # blank = unset (a key is generated into <data_dir>/secret.key)
        if len(raw) < MIN_SECRET_KEY_CHARS:
            raise ValueError(
                f"SALES0_SECRET_KEY must be at least {MIN_SECRET_KEY_CHARS} characters (or a Fernet key); "
                "generate one with: openssl rand -base64 48"
            )
        return raw

    @field_validator("admin_email", mode="before")
    @classmethod
    def _lower_admin_email(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("app_password", mode="before")
    @classmethod
    def _empty_password_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

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

    @field_validator("mode", mode="before")
    @classmethod
    def _mode_lower(cls, value: Any) -> Any:
        value = _clean_enum_value(value)
        return value.lower() if isinstance(value, str) else value

    @field_validator("worker_token", mode="before")
    @classmethod
    def _worker_token_strong(cls, value: Any) -> Any:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str):
            return value
        raw = raw.strip()
        if not raw:
            return None
        if len(raw) < MIN_WORKER_TOKEN_CHARS:
            raise ValueError(
                f"SALES0_WORKER_TOKEN must be at least {MIN_WORKER_TOKEN_CHARS} characters; generate one with: openssl rand -base64 36"
            )
        return raw

    @field_validator("data_dir", "database_url", "test_database_url", "log_file", "db_host", "worker_server_url", "worker_name", mode="before")
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
        if self.mode == "hosted":
            # No in-process timers on Cloud Run (CPU exists only during a request): everything is a manual action.
            object.__setattr__(self, "scheduler_enabled", False)
            object.__setattr__(self, "reply_poll_minutes", 0)
            object.__setattr__(self, "sheet_sync_minutes", 0)
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

    def _under_data_dir(self, value: str) -> Path:
        """Absolute paths stay; the default ``data/<file>`` follows SALES0_DATA_DIR; other relative paths use the repo root."""
        path = Path(value).expanduser()
        if path.is_absolute():
            return path
        if path.parts[:1] == ("data",):
            return self.data_path.joinpath(*path.parts[1:])
        return REPO_ROOT / path

    @property
    def google_client_secret_file(self) -> Path:
        return self._under_data_dir(self.google_client_secret_path)

    @property
    def google_token_file(self) -> Path:
        return self._under_data_dir(self.google_token_path)

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
        if self.db_host:
            from sqlalchemy.engine import URL

            return URL.create(
                "postgresql+psycopg",
                username=self.db_user,
                password=self.db_password.get_secret_value() if self.db_password else None,
                host=self.db_host,
                port=self.db_port,
                database=self.db_name,
                query={"sslmode": self.db_sslmode},
            ).render_as_string(hide_password=False)
        for old in ("postgres://", "postgresql://"):  # bare scheme -> the psycopg 3 driver we ship
            if url.startswith(old):
                return "postgresql+psycopg://" + url[len(old) :]
        prefix = "sqlite:///"
        if url.startswith(prefix) and not url.startswith("sqlite:////"):
            rest = url[len(prefix) :]
            if rest and rest != ":memory:" and not rest.startswith(":"):
                return f"{prefix}{(REPO_ROOT / rest).as_posix()}"
        return url

    @property
    def is_hosted(self) -> bool:
        return self.mode == "hosted"

    @property
    def is_sqlite(self) -> bool:
        return self.resolved_database_url.startswith("sqlite")

    @property
    def worker_token_value(self) -> str | None:
        return self.worker_token.get_secret_value() if self.worker_token else None

    @property
    def is_loopback_host(self) -> bool:
        if self.host in LOOPBACK_HOSTS:
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False

    @property
    def app_password_set(self) -> bool:
        return self.app_password is not None and bool(self.app_password.get_secret_value())

    @property
    def session_cookie_secure(self) -> bool:
        """Request-independent Secure decision (see ``sales0.web.cookies.cookie_secure`` for the per-request one)."""
        if self.cookie_secure != "auto":
            return self.cookie_secure == "true"
        return (self.public_base_url or "").lower().startswith("https://")

    @property
    def allowed_host_names(self) -> frozenset[str]:
        """Host names (no port) accepted once logins are enabled: loopback, SALES0_HOST, the public URL's host and
        SALES0_ALLOWED_HOSTS."""
        from urllib.parse import urlsplit

        names = {h.lower() for h in LOOPBACK_HOSTS} | {self.host.lower().strip("[]")}
        if self.public_base_url:
            try:
                public = (urlsplit(self.public_base_url).hostname or "").lower()
            except ValueError:
                public = ""
            if public:
                names.add(public)
        for part in self.allowed_hosts.split(","):
            part = part.strip().lower()
            if part:
                try:
                    names.add((urlsplit(f"//{part}").hostname or part).lower())
                except ValueError:
                    names.add(part)
        if self.allow_testserver_host:
            names.add("testserver")
        return frozenset(names)

    @property
    def open_mode_host_names(self) -> frozenset[str]:
        """Host names accepted while NO login exists (open mode): loopback and SALES0_HOST only."""
        names = {h.lower() for h in LOOPBACK_HOSTS} | {self.host.lower().strip("[]")}
        if self.allow_testserver_host:
            names.add("testserver")
        return frozenset(names)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class StartupError(RuntimeError):
    """A configuration problem that must stop the server before it binds."""


def _accounts_have_passwords(settings: Settings) -> bool:
    """True when some team member already has a password in the database (best effort: any error = False)."""
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(settings.resolved_database_url)
        try:
            with engine.connect() as conn:
                return bool(conn.execute(text("SELECT COUNT(*) FROM team_members WHERE password_hash IS NOT NULL")).scalar())
        finally:
            engine.dispose()
    except Exception:  # noqa: BLE001 - unmigrated / unreachable DB: treat as no accounts, the check stays strict
        return False


def _check_hosted(settings: Settings) -> None:
    """SALES0_MODE=hosted refuses to boot half-configured: open mode, SQLite, missing secrets."""
    problems: list[str] = []
    if settings.is_sqlite:
        problems.append("SALES0_DATABASE_URL (or SALES0_DB_HOST) must point at PostgreSQL, not SQLite")
    if settings.secret_key is None:
        problems.append("SALES0_SECRET_KEY is required (there is no persistent data dir to generate one into)")
    if not (settings.public_base_url or "").lower().startswith("https://"):
        problems.append("SALES0_PUBLIC_BASE_URL must be the https Cloud Run URL (Gmail OAuth redirect URI, secure cookies)")
    if settings.worker_token is None:
        problems.append("SALES0_WORKER_TOKEN is required (the laptop worker authenticates with it)")
    if not settings.app_password_set and not _accounts_have_passwords(settings):
        problems.append("hosted mode never runs in open mode: set SALES0_APP_PASSWORD (first admin's initial password)")
    if settings.linkedin_mode != "live":
        problems.append("SALES0_LINKEDIN_MODE=fixture is not allowed in hosted mode")
    if problems:
        raise StartupError("Refusing to boot SALES0_MODE=hosted: " + "; ".join(problems))


def check_startup(settings: Settings) -> None:
    """Fail loudly on configuration that must never reach a running server."""
    if not settings.api_key_present:
        raise StartupError(
            "SALES0_ANTHROPIC_API_KEY is missing — add it to .env in the repo root (see .env.example)."
        )
    if settings.is_hosted:
        _check_hosted(settings)
    if settings.linkedin_mode == "fixture" and not settings.is_loopback_host:
        raise StartupError(
            "Refusing SALES0_LINKEDIN_MODE=fixture on a non-loopback SALES0_HOST: it serves a fake LinkedIn "
            "(/__fixtures/*) without login. Use fixture mode on 127.0.0.1 only."
        )
    if not settings.is_loopback_host and not settings.app_password_set and not _accounts_have_passwords(settings):
        raise StartupError(
            "Refusing to bind to a public host in open mode: set SALES0_APP_PASSWORD (the first admin's "
            "initial password) or give an admin a password on the Users page first"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
