"""LinkedIn accounts: the single data-access layer for ``linkedin_accounts`` (Phase 5).

Everything that used to read or write the ``linkedin_session`` singleton goes through here: list /
add / rename / disable / enable, status transitions (connected / not connected / needs attention),
pins (proxy, user agent, locale, timezone, viewport), the safety state and daily-counter snapshot of
ONE account, the encrypted ``session_state`` of db-backend accounts, and the paste-``li_at``-cookie
path. ``default_account_id`` is the account every entry point uses when none is named.

Queries on ``scrape_runs`` that depend on the account (cool-down, daily caps) live in
``services.runs`` and filter through ``account_runs_clause`` below, which treats an unstamped
(``linkedin_account_id IS NULL``) run as belonging to the default account.

``linkedin_session`` (legacy singleton) is never read or written any more; the p8 migration copied it
into the default account.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.orm import Session

from sales0.clock import utcnow
from sales0.config import Settings, get_settings
from sales0.db.engine import session_scope
from sales0.db.models import LinkedInAccount, ScrapeRun
from sales0.errors import DomainError
from sales0.logging import get_logger
from sales0.services._common import clip, not_found, now_or, record_history

if TYPE_CHECKING:
    from sales0.scraper.session_store import AccountHandle, AccountSpec

log = get_logger("sales0.services.linkedin_accounts")

DEFAULT_LABEL = "Default"
LABEL_MAX = 80
HISTORY_ENTITY = "linkedin_session"  # status_history entity type kept for compat (entity_id = account id)
STORED_STATUSES = ("not_connected", "connected", "needs_attention")

COOKIE_MIN_LEN = 8
COOKIE_MAX_LEN = 1500
COOKIE_INVALID = "That does not look like a li_at cookie value (no spaces, quotes or semicolons, 8-1500 characters)."
SECRET_KEY_REQUIRED = (
    "SALES0_SECRET_KEY must be set before a LinkedIn cookie can be stored on a non-local server "
    "- the cookie is kept encrypted with it."
)

# Playwright storage_state cookie lifetime for a pasted cookie (LinkedIn's own li_at lasts about a year).
COOKIE_LIFETIME_S = 365 * 24 * 3600


class LinkedInAccountView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    label: str
    owner_member_id: int | None
    status: str  # not_connected | connected | needs_attention | disabled
    stored_status: str  # the status underneath "disabled"
    disabled: bool
    backend: str
    is_default: bool
    has_session_state: bool
    last_checked_at: datetime | None
    last_connected_at: datetime | None
    last_successful_run_at: datetime | None
    last_ok_at: datetime | None
    last_error: str | None
    safety_state: str | None
    last_stop_at: datetime | None
    cooldown_until: datetime | None
    proxy_set: bool
    user_agent: str | None
    locale: str | None
    timezone: str | None
    viewport: str | None
    runs_today: int
    posts_today: int


# ---------------------------------------------------------------------- lookup
def get_account(db: Session, account_id: int) -> LinkedInAccount:
    row = db.get(LinkedInAccount, account_id)
    if row is None:
        raise not_found()
    return row


def _first_active(db: Session) -> LinkedInAccount | None:
    return db.scalar(
        select(LinkedInAccount).where(LinkedInAccount.disabled.is_(False)).order_by(LinkedInAccount.id).limit(1)
    )


def ensure_default_account(db: Session) -> LinkedInAccount:
    """The first active account; recreates the default one defensively when none exists at all."""
    row = _first_active(db)
    if row is not None:
        return row
    if db.scalar(select(LinkedInAccount.id).limit(1)) is not None:
        raise DomainError("no_active_account", "Every LinkedIn account is disabled - enable one in Setup.", 409)
    row = LinkedInAccount(label=DEFAULT_LABEL, status="not_connected", backend="folder")
    db.add(row)
    db.flush()
    return row


def default_account_id(db: Session) -> int:
    """The account used when an entry point is not told which one: the first active account (by id)."""
    return ensure_default_account(db).id


def resolve_account(db: Session, account_id: int | None) -> LinkedInAccount:
    return ensure_default_account(db) if account_id is None else get_account(db, account_id)


def account_runs_clause(db: Session, account_id: int) -> ColumnElement[bool]:
    """``scrape_runs`` rows that belong to ``account_id`` (unstamped rows belong to the default account)."""
    mine = ScrapeRun.linkedin_account_id == account_id
    first = _first_active(db)
    if first is not None and first.id == account_id:
        return or_(mine, ScrapeRun.linkedin_account_id.is_(None))
    return mine


# ---------------------------------------------------------------------- views / list
def _view(row: LinkedInAccount, default_id: int | None) -> LinkedInAccountView:
    stored = row.status if row.status in STORED_STATUSES else "not_connected"
    return LinkedInAccountView(
        id=row.id,
        label=row.label,
        owner_member_id=row.owner_member_id,
        status="disabled" if row.disabled else stored,
        stored_status=stored,
        disabled=row.disabled,
        backend=row.backend,
        is_default=row.id == default_id,
        has_session_state=bool(row.session_state),
        last_checked_at=row.last_checked_at,
        last_connected_at=row.last_connected_at,
        last_successful_run_at=row.last_successful_run_at,
        last_ok_at=row.last_ok_at,
        last_error=row.last_error,
        safety_state=row.safety_state,
        last_stop_at=row.last_stop_at,
        cooldown_until=row.cooldown_until,
        proxy_set=bool(row.proxy_url),
        user_agent=row.user_agent,
        locale=row.locale,
        timezone=row.timezone,
        viewport=row.viewport,
        runs_today=row.runs_today,
        posts_today=row.posts_today,
    )


def list_accounts(db: Session, *, include_disabled: bool = True) -> list[LinkedInAccountView]:
    query = select(LinkedInAccount).order_by(LinkedInAccount.id)
    if not include_disabled:
        query = query.where(LinkedInAccount.disabled.is_(False))
    rows = db.scalars(query).all()
    first = _first_active(db)
    default_id = first.id if first else None
    return [_view(r, default_id) for r in rows]


def account_view(db: Session, account_id: int) -> LinkedInAccountView:
    row = get_account(db, account_id)
    first = _first_active(db)
    return _view(row, first.id if first else None)


# ---------------------------------------------------------------------- add / rename / disable
def _clean_label(label: str | None) -> str:
    text = " ".join((label or "").split())
    if not text:
        raise DomainError("account_label_required", "Give the LinkedIn account a name.", 400)
    if len(text) > LABEL_MAX:
        raise DomainError("account_label_invalid", f"The account name can be at most {LABEL_MAX} characters.", 400)
    return text


def add_account(
    db: Session,
    label: str,
    *,
    backend: str | None = None,
    owner_member_id: int | None = None,
    settings: Settings | None = None,
) -> LinkedInAccountView:
    """A new, not-yet-connected account. ``backend`` defaults to ``SALES0_LINKEDIN_SESSION_BACKEND``."""
    settings = settings or get_settings()
    chosen = backend or settings.linkedin_session_backend
    if chosen not in ("folder", "db"):
        raise DomainError("account_backend_invalid", "The session backend must be 'folder' or 'db'.", 400)
    clean = _clean_label(label)
    row = LinkedInAccount(label=clean, backend=chosen, owner_member_id=owner_member_id, status="not_connected")
    db.add(row)
    db.flush()
    if chosen == "folder":
        row.profile_dirname = f"linkedin-account-{row.id}"
    db.flush()
    return account_view(db, row.id)


def rename_account(db: Session, account_id: int, label: str) -> LinkedInAccountView:
    row = get_account(db, account_id)
    row.label = _clean_label(label)
    db.flush()
    return account_view(db, account_id)


def disable_account(db: Session, account_id: int) -> LinkedInAccountView:
    """Disable an account (kept, never deleted). The last active account cannot be disabled."""
    row = get_account(db, account_id)
    if not row.disabled:
        others = db.scalar(
            select(LinkedInAccount.id).where(LinkedInAccount.disabled.is_(False), LinkedInAccount.id != account_id).limit(1)
        )
        if others is None:
            raise DomainError(
                "last_account", "At least one LinkedIn account must stay enabled - add another one first.", 409
            )
        row.disabled = True
    db.flush()
    return account_view(db, account_id)


def enable_account(db: Session, account_id: int) -> LinkedInAccountView:
    row = get_account(db, account_id)
    row.disabled = False
    db.flush()
    return account_view(db, account_id)


def set_owner(db: Session, account_id: int, owner_member_id: int | None) -> LinkedInAccountView:
    get_account(db, account_id).owner_member_id = owner_member_id
    db.flush()
    return account_view(db, account_id)


_VIEWPORT = re.compile(r"^(\d{3,5})x(\d{3,5})$")


def parse_viewport(value: str | None) -> tuple[int, int] | None:
    match = _VIEWPORT.match((value or "").strip().lower())
    return (int(match.group(1)), int(match.group(2))) if match else None


def set_pins(
    db: Session,
    account_id: int,
    *,
    proxy_url: str | None = None,
    user_agent: str | None = None,
    locale: str | None = None,
    timezone: str | None = None,
    viewport: str | None = None,
) -> LinkedInAccountView:
    """Pin the browser identity of one account (blank clears a pin)."""
    row = get_account(db, account_id)
    if proxy_url and proxy_url.strip():
        parts = urlsplit(proxy_url.strip())
        if parts.scheme not in ("http", "https", "socks5") or not parts.hostname:
            raise DomainError("proxy_invalid", "Proxy URL must look like http://host:port or socks5://host:port.", 400)
    if viewport and viewport.strip() and parse_viewport(viewport) is None:
        raise DomainError("viewport_invalid", "Viewport must look like 1280x860.", 400)
    row.proxy_url = clip(proxy_url, 300)
    row.user_agent = clip(user_agent, 300)
    row.locale = clip(locale, 20)
    row.timezone = clip(timezone, 60)
    row.viewport = clip(viewport.lower() if viewport else None, 20)
    db.flush()
    return account_view(db, account_id)


# ---------------------------------------------------------------------- session status transitions
SAFETY_KINDS = ("logout", "checkpoint", "captcha", "rate_limited")


def _set_status(db: Session, row: LinkedInAccount, status: str, *, note: str | None, now: datetime) -> None:
    if row.status != status:
        record_history(db, HISTORY_ENTITY, row.id, "status", row.status, status, note=note, now=now)
        row.status = status
    row.updated_at = now


def mark_connected(db: Session, account_id: int | None = None, *, now: datetime | None = None, connected: bool = False) -> None:
    now = now_or(now)
    row = resolve_account(db, account_id)
    _set_status(db, row, "connected", note="connect" if connected else "check", now=now)
    row.last_checked_at = now
    row.last_ok_at = now
    if connected:
        row.last_connected_at = now
    row.last_error = None
    db.flush()


def mark_not_connected(db: Session, message: str | None, account_id: int | None = None, *, now: datetime | None = None) -> None:
    now = now_or(now)
    row = resolve_account(db, account_id)
    _set_status(db, row, "not_connected", note=message, now=now)
    row.last_checked_at = now
    row.last_error = clip(message, 300)
    db.flush()


def mark_needs_attention(
    db: Session, kind: str, message: str, account_id: int | None = None, *, now: datetime | None = None
) -> None:
    now = now_or(now)
    row = resolve_account(db, account_id)
    _set_status(db, row, "needs_attention", note=kind, now=now)
    row.last_checked_at = now
    row.last_error = clip(message, 300)
    row.safety_state = kind if kind in SAFETY_KINDS else row.safety_state
    row.last_stop_at = now
    row.last_stop_reason = clip(message, 300)
    db.flush()


def note_error(db: Session, message: str, account_id: int | None = None, *, now: datetime | None = None) -> None:
    """Record a non-state-changing error (e.g. the check itself crashed)."""
    now = now_or(now)
    row = resolve_account(db, account_id)
    row.last_checked_at = now
    row.last_error = clip(message, 300)
    row.updated_at = now
    db.flush()


def mark_successful_run(db: Session, account_id: int | None = None, *, now: datetime | None = None) -> None:
    now = now_or(now)
    row = resolve_account(db, account_id)
    row.last_successful_run_at = now
    row.last_ok_at = now
    db.flush()


def store_counters(
    db: Session, account_id: int, *, day: str, runs_today: int, posts_today: int, cooldown_until: datetime | None
) -> None:
    """Snapshot this account's derived daily counters and cool-down (see ``services.runs.sync_account_state``)."""
    row = get_account(db, account_id)
    row.counter_day = day
    row.runs_today = runs_today
    row.posts_today = posts_today
    row.cooldown_until = cooldown_until
    db.flush()


# ---------------------------------------------------------------------- session state (db backend)
def _encrypt_state(state_json: str) -> str:
    from sales0.services.team.crypto import encrypt

    return encrypt(state_json)


def decrypt_state(row: LinkedInAccount) -> str | None:
    """The account's storage_state JSON, decrypted; None when absent or undecryptable (never raises)."""
    if not row.session_state:
        return None
    from sales0.services.team.crypto import DecryptionError, decrypt

    try:
        return decrypt(row.session_state)
    except DecryptionError:
        log.warning("linkedin_session_state_undecryptable", account=row.id)
        return None


def save_session_state(db: Session, account_id: int, state_json: str) -> None:
    """Store a refreshed storage_state (encrypted). Only db-backend accounts keep one."""
    row = get_account(db, account_id)
    if row.backend != "db":
        return
    row.session_state = _encrypt_state(state_json)
    db.flush()


def clear_session_state(db: Session, account_id: int) -> None:
    row = get_account(db, account_id)
    row.session_state = None
    db.flush()


def spec_for(row: LinkedInAccount) -> AccountSpec:
    from sales0.scraper.session_store import AccountSpec

    return AccountSpec(
        id=row.id,
        backend=row.backend,
        profile_dirname=row.profile_dirname,
        storage_state=decrypt_state(row) if row.backend == "db" else None,
        proxy_url=row.proxy_url,
        user_agent=row.user_agent,
        locale=row.locale,
        timezone=row.timezone,
        viewport=parse_viewport(row.viewport),
    )


def _save_in_new_session(account_id: int, state_json: str) -> None:
    with session_scope() as db:
        save_session_state(db, account_id, state_json)


def handle_for(db: Session, account_id: int | None = None) -> AccountHandle:
    """The scraper's description of one account (db accounts get a saver that writes encrypted state)."""
    from sales0.scraper.session_store import AccountHandle

    row = resolve_account(db, account_id)
    spec = spec_for(row)
    if row.backend != "db":
        return AccountHandle(spec)
    aid = row.id
    return AccountHandle(spec, save_state=lambda state: _save_in_new_session(aid, state))


def build_handle(account_id: int | None = None) -> AccountHandle:
    """``handle_for`` in its own short session (for jobs that hold no session while the browser runs)."""
    with session_scope() as db:
        return handle_for(db, account_id)


# ---------------------------------------------------------------------- paste li_at cookie
def clean_cookie(raw: str | None) -> str:
    """The bare cookie value from whatever the user pasted (``li_at=...``, quotes, trailing ``;``)."""
    value = (raw or "").strip()
    if value.lower().startswith("li_at="):
        value = value[6:]
    value = value.strip().strip(";").strip().strip('"').strip("'")
    if not (COOKIE_MIN_LEN <= len(value) <= COOKIE_MAX_LEN) or re.search(r"[\s;\"'\\]", value):
        raise DomainError("cookie_invalid", COOKIE_INVALID, 400)
    return value


def storage_state_for_cookie(cookie: str, settings: Settings | None = None) -> dict[str, Any]:
    """A Playwright ``storage_state`` holding just the ``li_at`` cookie for the current LinkedIn base."""
    from sales0.scraper.urls import base_url

    settings = settings or get_settings()
    host = urlsplit(base_url(settings)).hostname or "www.linkedin.com"
    live = settings.linkedin_mode == "live"
    return {
        "cookies": [
            {
                "name": "li_at",
                "value": cookie,
                "domain": ".linkedin.com" if live else host,
                "path": "/",
                "expires": float(int(utcnow().timestamp()) + COOKIE_LIFETIME_S),
                "httpOnly": True,
                "secure": live,
                "sameSite": "None" if live else "Lax",
            }
        ],
        "origins": [],
    }


def _require_secret_key(settings: Settings) -> None:
    if settings.secret_key is not None and settings.secret_key.get_secret_value().strip():
        return
    if settings.host in ("127.0.0.1", "localhost", "::1"):
        return  # local dev: crypto.master_secret persists a generated key under the data dir (with a warning)
    raise DomainError("secret_key_required", SECRET_KEY_REQUIRED, 400)


def paste_cookie(
    db: Session,
    cookie: str,
    *,
    label: str | None = None,
    account_id: int | None = None,
    owner_member_id: int | None = None,
    settings: Settings | None = None,
) -> LinkedInAccountView:
    """Store a pasted ``li_at`` on a db-backend account (new, or ``account_id``'s) - NOT yet validated.

    The account is left ``not_connected``; the caller then runs the check job (one safe feed load) which
    flips it to ``connected`` or reports why not. A folder account cannot take a cookie (its login lives
    in the Chrome profile).
    """
    settings = settings or get_settings()
    from sales0.services._common import require_local_browser

    require_local_browser("Pasting a LinkedIn cookie")  # cookies never leave the laptop
    value = clean_cookie(cookie)
    _require_secret_key(settings)
    if account_id is None:
        row = LinkedInAccount(
            label=_clean_label(label or "Pasted cookie"),
            backend="db",
            status="not_connected",
            owner_member_id=owner_member_id,
        )
        db.add(row)
        db.flush()
    else:
        row = get_account(db, account_id)
        if row.backend != "db":
            raise DomainError(
                "account_backend_mismatch",
                "That account logs in through its Chrome window - add a new account to use a pasted cookie.",
                409,
            )
    row.session_state = _encrypt_state(json.dumps(storage_state_for_cookie(value, settings)))
    _set_status(db, row, "not_connected", note="cookie_pasted", now=utcnow())
    row.last_error = None
    db.flush()
    log.info("linkedin_cookie_stored", account=row.id)  # never the value
    return account_view(db, row.id)


__all__ = [
    "COOKIE_INVALID",
    "DEFAULT_LABEL",
    "LinkedInAccountView",
    "SAFETY_KINDS",
    "account_runs_clause",
    "account_view",
    "add_account",
    "build_handle",
    "clean_cookie",
    "clear_session_state",
    "decrypt_state",
    "default_account_id",
    "disable_account",
    "enable_account",
    "ensure_default_account",
    "get_account",
    "handle_for",
    "list_accounts",
    "mark_connected",
    "mark_needs_attention",
    "mark_not_connected",
    "mark_successful_run",
    "note_error",
    "parse_viewport",
    "paste_cookie",
    "rename_account",
    "resolve_account",
    "save_session_state",
    "set_owner",
    "set_pins",
    "spec_for",
    "storage_state_for_cookie",
    "store_counters",
]
