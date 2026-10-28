"""Generate the README table of every ``SALES0_*`` setting (``python -m sales0.cli env-docs``).

The help text lives here (``HELP``) rather than in config.py so that docs wording never touches
the settings model; a unit test fails when a Settings field has no entry or the README block
drifts from this output.
"""

from __future__ import annotations

import typing
from pathlib import Path
from typing import Any, get_args, get_origin

from pydantic import SecretStr
from pydantic_core import PydanticUndefined

from sales0.config import Settings

START = "<!-- env-docs:start -->"
END = "<!-- env-docs:end -->"

HELP: dict[str, str] = {
    "anthropic_api_key": "Anthropic API key for triage, research, drafting and the digest summary. Required to start the server.",
    "anthropic_workspace_id": "Only when the key is not scoped to a workspace: sent as the `anthropic-workspace-id` header.",
    "llm_model": "Model slug used for every LLM call.",
    "llm_max_output_tokens": "Max output tokens per LLM call.",
    "llm_timeout_s": "Per-call LLM timeout in seconds.",
    "llm_max_retries": "Retries on transient LLM errors.",
    "llm_price_input_usd_per_mtok": "Input price (USD per 1M tokens) used for the spend estimate.",
    "llm_price_output_usd_per_mtok": "Output price (USD per 1M tokens) used for the spend estimate.",
    "host": "Bind address. A non-loopback host refuses to start without `SALES0_APP_PASSWORD` (or an existing user account).",
    "port": "Bind port.",
    "data_dir": "Runtime directory: SQLite DB, Chrome profile, secret key, uploads. Put it on a persistent volume when hosted.",
    "database_url": "SQLAlchemy URL. SQLite only today; relative file paths resolve against the repo root. Postgres is not supported yet.",
    "test_database_url": "Test-suite only: database URL used by tests instead of a temp file.",
    "google_client_secret_path": "Google OAuth client JSON (Desktop `installed` or Web `web` type) used for Gmail.",
    "google_token_path": "Token file written by the `google-auth` CLI command (single-user fallback; team members' tokens live encrypted in the DB).",
    "secret_key": "Encrypts Gmail tokens and LinkedIn sessions at rest and signs cookies (>= 16 chars or a Fernet key). Unset: generated once into `<data_dir>/secret.key`. Set it explicitly on a server and back it up: losing it makes stored tokens unreadable.",
    "admin_email": "Email of the first admin team member created on an empty database.",
    "admin_name": "Display name of that first admin.",
    "gmail_daily_send_cap": "Max outreach emails sent per member per day (0 disables sending).",
    "deck_max_bytes": "Max size of one uploaded deck PDF, in bytes.",
    "public_base_url": "Public origin when hosted (e.g. `https://sales.example.com`). Builds the Google OAuth redirect (`<url>/oauth/google/callback`, must match a registered redirect URI exactly) and (with `SALES0_COOKIE_SECURE=auto`) turns on the Secure cookie flag when it starts with `https://`; its host is added to the accepted Host names.",
    "allowed_hosts": "Extra Host names (comma separated, any port) accepted once logins are enabled, in addition to loopback, `SALES0_HOST` and the host of `SALES0_PUBLIC_BASE_URL`. Any other Host header is refused with 400 (DNS-rebinding guard). In open mode (no logins) only loopback and `SALES0_HOST` are accepted.",
    "cookie_secure": "Secure flag on every cookie: `auto` (default: when the request is https, or `X-Forwarded-Proto: https` arrives from a `SALES0_TRUSTED_PROXIES` peer, or `SALES0_PUBLIC_BASE_URL` is https), `true` or `false`.",
    "allow_testserver_host": "TEST ONLY: also accept the Host `testserver` (Starlette TestClient). Never set it on a server.",
    "google_auth_endpoint": "Google authorize endpoint (override for tests/proxies).",
    "google_token_endpoint": "Google token endpoint (override for tests/proxies).",
    "google_userinfo_endpoint": "Google userinfo endpoint (override for tests/proxies).",
    "google_revoke_endpoint": "Google revoke endpoint (override for tests/proxies).",
    "research_allow_private_hosts": "Test only: hosts the research fetcher may reach although private. Ignored unless `SALES0_LINKEDIN_MODE=fixture`.",
    "timezone": "IANA timezone for the scheduler, digest and daily caps.",
    "log_level": "Log level.",
    "log_format": "`json` or `console`.",
    "log_file": "Also write logs to this file (optional).",
    "linkedin_mode": "`live` drives the real LinkedIn; `fixture` uses the local fake site (tests/demo).",
    "linkedin_browser_channel": "`auto` (installed Chrome, else bundled Chromium), `chrome`, or `chromium`.",
    "linkedin_connect_timeout_s": "How long the headed LinkedIn login window may stay open.",
    "safety_cooldown_min": "Minutes a LinkedIn account stays paused after a rate-limit/captcha stop (0 = off).",
    "safety_cooldown_checkpoint_min": "Minutes paused after a checkpoint/logout stop.",
    "scrape_max_keywords_per_run": "Max search phrases per discovery run.",
    "scrape_max_posts_per_keyword": "Max posts read per phrase.",
    "scrape_max_posts_per_day": "Max posts read per day.",
    "scrape_max_runs_per_day": "Max discovery runs per day.",
    "scrape_delay_min_s": "Min human-like delay between actions (seconds).",
    "scrape_delay_max_s": "Max delay between actions (seconds).",
    "scrape_keyword_pause_min_s": "Min pause between phrases (seconds).",
    "scrape_keyword_pause_max_s": "Max pause between phrases (seconds).",
    "scrape_max_scrolls": "Max scrolls per search pass.",
    "scrape_no_growth_scrolls": "Consecutive scrolls without new cards that end a pass.",
    "scrape_sort": "`latest` or `relevance` search ordering.",
    "scrape_date_filters": "Comma list of date filters, one search pass each: `past-24h`, `past-week`, `past-month`.",
    "scrape_date_filter_legacy": "Legacy single date filter; read only when `SALES0_SCRAPE_DATE_FILTERS` is unset.",
    "scrape_variants": "Run extra per-phrase search variants in a daily rotation.",
    "scrape_max_passes_per_keyword": "Cap on search passes per phrase.",
    "scrape_expand_max_per_keyword": "Max \"see more\" clicks per phrase.",
    "scrape_permalink_max_per_keyword": "Max \"Copy link to post\" attempts per phrase (0 = off).",
    "fewshot_max_examples": "Max reviewed examples shown to triage as few-shot context.",
    "fewshot_text_chars": "Characters kept per few-shot example.",
    "triage_max_input_chars": "Characters of a post sent to triage.",
    "triage_concurrency": "Parallel triage calls.",
    "seen_raw_retention_days": "Days raw seen-post text is kept.",
    "seed_suggested_keywords": "Seed the suggested search phrases on first run.",
    "reply_poll_minutes": "Minutes between Gmail reply checks (0 disables the poller).",
    "reply_lookback_days": "Only sent messages younger than this are polled for replies.",
    "reply_max_threads_per_poll": "Max threads checked per member per cycle.",
    "reply_own_aliases": "Comma list of extra addresses that are \"us\" (Send-As aliases); never counted as replies.",
    "sheet_sync_minutes": "Minutes between Google Sheet syncs (0 disables).",
    "sheet_client_sourcing_id": "Spreadsheet id of the client-sourcing sheet.",
    "sheet_metrics_id": "Spreadsheet id of the metrics sheet.",
    "sheet_tabs_json": "Optional JSON list replacing the default tabs: `[{\"name\",\"kind\",\"gid\",\"sheet_id\"?}]`.",
    "sheet_fetch_timeout_s": "Sheet fetch timeout in seconds.",
    "sheet_fetch_retries": "Sheet fetch retries.",
    "sheet_fetch_max_bytes": "Max bytes read from a sheet export.",
    "app_password": "Bootstrap password: creates the first admin account when no users exist (and is the shared login until then). Unset = open mode, loopback only. Not a per-user password.",
    "session_days": "Login session lifetime in days.",
    "login_max_attempts": "Failed logins per client before lockout.",
    "login_lockout_s": "Lockout duration in seconds.",
    "session_remember_days": "Lifetime of a \"remember me\" login cookie, in days.",
    "trusted_proxies": "Comma list of IPs/CIDRs of reverse proxies whose `X-Forwarded-For` is trusted for client IPs (login throttle). Empty = never trust it.",
    "password_reset_ttl_h": "Hours an admin-issued set-password link stays valid.",
    "password_min_length": "Minimum length of a user password.",
    "linkedin_session_backend": "Where NEW LinkedIn accounts keep their session: `folder` (Chrome profile under the data dir) or `db` (encrypted storage state).",
    "followup_days_1": "Days after the first email before follow-up 1 is due.",
    "followup_days_2": "Days before follow-up 2.",
    "followup_days_final": "Days before the final follow-up.",
    "stale_lead_days": "Days without activity before a lead counts as stale.",
    "repeat_poster_min": "Posts needed to flag someone as a repeat poster.",
    "repeat_poster_days": "Window (days) for the repeat-poster count.",
    "crm_page_size": "Rows per page in CRM lists.",
    "monthly_spend_cap_usd": "Monthly AI spend cap in USD (0 = no cap). Over the cap every LLM feature stops.",
    "spend_cap_warn_pct": "Percent of the cap at which a warning shows.",
    "scheduler_enabled": "False = no background scheduler thread (pages still work).",
    "scheduler_poll_s": "Scheduler tick in seconds.",
    "scheduler_window_start": "Nightly discovery window start (local `HH:MM`).",
    "scheduler_window_end": "Nightly discovery window end (local `HH:MM`, same day).",
    "scheduler_discovery_max_runs_per_day": "Scheduled discovery runs per day (also bounded by `SALES0_SCRAPE_MAX_RUNS_PER_DAY`).",
    "scheduler_digest_time": "Local time the daily digest is built.",
    "scheduler_purge_time": "Local time of the nightly purge.",
    "scheduler_watch_time": "Local time the `profile_watch` job (visit tracked profiles) runs; the job is disabled until enabled on /schedule.",
    "watch_max_profiles_per_run": "Prospect watch: max tracked profiles visited per watch run.",
    "watch_min_days_between_checks": "Prospect watch: minimum days before the same profile is visited again.",
    "watch_max_posts_per_profile": "Prospect watch: max posts read per profile visit (also bounded by the daily post cap).",
    "watch_profile_pause_min_s": "Prospect watch: minimum pause between two profile visits (seconds).",
    "watch_profile_pause_max_s": "Prospect watch: maximum pause between two profile visits (seconds).",
    "watch_append_to_discovery": "True = also visit due tracked profiles at the end of each discovery run (same browser session and caps).",
    "purge_retention_days": "Days of raw data kept before purge.",
    "digest_ai_summary": "One LLM call adds a short summary to the digest.",
}


def _type_name(annotation: Any) -> str:
    if _is_secret(annotation):
        return "secret"
    origin = get_origin(annotation)
    if origin is typing.Literal:
        return " | ".join(str(a) for a in get_args(annotation))
    if origin is typing.Annotated:
        return _type_name(get_args(annotation)[0])
    args = [a for a in get_args(annotation) if a is not type(None)]
    if origin in (typing.Union, getattr(__import__("types"), "UnionType", None)) and len(args) == 1:
        return _type_name(args[0])
    if origin is tuple:
        return "list"
    return getattr(annotation, "__name__", str(annotation))


def _is_secret(annotation: Any) -> bool:
    return SecretStr in get_args(annotation) or annotation is SecretStr


def env_name(name: str) -> str:
    field = Settings.model_fields[name]
    alias = field.validation_alias
    return alias if isinstance(alias, str) else f"SALES0_{name.upper()}"


def missing_help() -> list[str]:
    return [n for n in Settings.model_fields if not HELP.get(n, "").strip()]


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _default(name: str) -> str:
    field = Settings.model_fields[name]
    if _is_secret(field.annotation):
        return "(secret)"
    if field.default is PydanticUndefined or field.default is None:
        return "unset"
    value = field.default
    if isinstance(value, tuple):
        value = ",".join(value)
    if value == "":
        return "empty"
    return f"`{value}`"


def render_table() -> str:
    lines = ["| Variable | Type | Default | Meaning |", "| --- | --- | --- | --- |"]
    for name in Settings.model_fields:
        field = Settings.model_fields[name]
        lines.append(
            f"| `{env_name(name)}` | {_cell(_type_name(field.annotation))} | {_cell(_default(name))} | {_cell(HELP.get(name, '').strip())} |"
        )
    return "\n".join(lines)


def render_block() -> str:
    return f"{START}\n\n{render_table()}\n\n{END}"


def _split(text: str) -> tuple[str, str, str]:
    if START not in text or END not in text:
        raise ValueError(f"missing {START} / {END} markers")
    head, rest = text.split(START, 1)
    body, tail = rest.split(END, 1)
    return head, body, tail


def current_block(path: Path) -> str:
    _, body, _ = _split(path.read_text("utf-8"))
    return f"{START}{body}{END}"


def is_current(path: Path) -> bool:
    return current_block(path) == render_block()


def write_block(path: Path) -> bool:
    """Rewrite the block in ``path``; True when the file changed."""
    text = path.read_text("utf-8")
    head, _, tail = _split(text)
    new = head + render_block() + tail
    if new == text:
        return False
    path.write_text(new, "utf-8")
    return True
