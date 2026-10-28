"""LinkedIn session status of ONE account (default: the first active account) + connect / re-check job starts.

Since p8 the state lives in ``linkedin_accounts`` (``services.linkedin_accounts``); this module keeps the
Setup-card facade and the old function names, each taking an optional ``account_id``.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from sales0.clock import utcnow
from sales0.config import get_settings
from sales0.errors import DomainError
from sales0.db.models import LinkedInAccount
from sales0.jobs.runner import get_runner
from sales0.logging import get_logger
from sales0.services import linkedin_accounts as accounts
from sales0.services._common import clip
from sales0.services.views import SessionStatusView

log = get_logger("sales0.services.session")

# Phase 1.1 wording: the user resolves a stop in sales0's own Chrome window, which
# Connect LinkedIn opens on LinkedIn (never "a normal browser" — that has no sales0 login).
_SAFETY_TEMPLATES: dict[str, str] = {
    "logout": "Logged out of LinkedIn (login page at {path}) — press Connect LinkedIn to log in again",
    "checkpoint": "Checkpoint page at {path} — press Connect LinkedIn and resolve it in the sales0 Chrome window",
    "captcha": "Captcha shown at {path} — press Connect LinkedIn and solve it in the sales0 Chrome window",
    "rate_limited": "LinkedIn rate-limited the session at {path} — wait a few hours, then Re-check",
}

# Data flow step 2: the profile has no li_at cookie at all (not a safety stop).
NOT_LOGGED_IN_MESSAGE = "Not logged in to LinkedIn in sales0's browser — press Connect LinkedIn in Setup"
CHROME_MISSING_LABEL = "Google Chrome (not installed)"


def _path(url: str | None) -> str:
    if not url:
        return "an unknown page"
    parts = urlsplit(url)
    return parts.path or url


def safety_message(kind: str, url: str | None) -> str:
    template = _SAFETY_TEMPLATES.get(kind, "LinkedIn stopped the session ({kind}) at {path}")
    return clip(template.format(path=_path(url), kind=kind), 300) or ""


def get_session_row(db: Session, account_id: int | None = None) -> LinkedInAccount:
    """The ORM row of a LinkedIn account (None = the default account). Compat name; the singleton is gone."""
    return accounts.resolve_account(db, account_id)


def mark_connected(
    db: Session, *, now: datetime | None = None, connected: bool = False, account_id: int | None = None
) -> None:
    accounts.mark_connected(db, account_id, now=now, connected=connected)


def mark_not_connected(
    db: Session, message: str | None, *, now: datetime | None = None, account_id: int | None = None
) -> None:
    accounts.mark_not_connected(db, message, account_id, now=now)


def mark_needs_attention(
    db: Session, kind: str, url: str | None, *, now: datetime | None = None, account_id: int | None = None
) -> str:
    message = safety_message(kind, url)
    accounts.mark_needs_attention(db, kind, message, account_id, now=now)
    return message


def note_error(db: Session, message: str, *, now: datetime | None = None, account_id: int | None = None) -> None:
    """Record a non-state-changing error (e.g. the check itself crashed)."""
    accounts.note_error(db, message, account_id, now=now)


def mark_successful_run(db: Session, *, now: datetime | None = None, account_id: int | None = None) -> None:
    accounts.mark_successful_run(db, account_id, now=now)


def browser_info() -> tuple[str, str | None, str | None]:
    """(label, note, channel) for the Setup browser line; never raises.

    Called through the module attribute so tests can stub ``resolve_browser``.
    """
    from sales0.scraper import browser as browser_mod
    from sales0.scraper.errors import BrowserUnavailable

    try:
        choice = browser_mod.resolve_browser(get_settings())
    except BrowserUnavailable as exc:
        return CHROME_MISSING_LABEL, str(exc) or None, None
    except Exception as exc:  # noqa: BLE001 — a detection bug must not 500 every page
        log.exception("browser_resolve_failed", error_type=type(exc).__name__)
        return "Browser unknown", "Could not detect the browser — see the server log", None
    return choice.label, choice.note, choice.channel


def browser_channel() -> str | None:
    """The resolved channel for log lines (``chrome|chromium``), or None when unavailable."""
    try:
        return browser_info()[2]
    except Exception:  # noqa: BLE001 — logging must never fail a job
        return None


def session_status(db: Session, account_id: int | None = None) -> SessionStatusView:
    from sales0.services.runs import safety_cooldown_until

    row = get_session_row(db, account_id)
    runner = get_runner()
    busy = runner.busy()
    stored = row.status if row.status in ("not_connected", "connected", "needs_attention") else "not_connected"
    label, note, channel = browser_info()
    return SessionStatusView(
        status="busy" if busy else stored,  # type: ignore[arg-type]
        busy_action=busy,  # type: ignore[arg-type]
        last_checked_at=row.last_checked_at,
        last_successful_run_at=row.last_successful_run_at,
        last_error=row.last_error,
        mode=get_settings().linkedin_mode,
        connect_elapsed_s=runner.connect_elapsed_s() if busy == "connect" else None,
        stored_status=stored,  # type: ignore[arg-type]
        last_connected_at=row.last_connected_at,
        browser_label=label,
        browser_note=note,
        browser_channel=channel,  # type: ignore[arg-type]
        cooldown_until=safety_cooldown_until(db, account_id=row.id),
    )


def start_connect(db: Session, account_id: int | None = None) -> SessionStatusView:
    from sales0.jobs import connect_job

    row = get_session_row(db, account_id)
    if row.disabled:
        raise DomainError("account_disabled", "That LinkedIn account is disabled - enable it first.", 409)
    if row.backend == "db":
        raise DomainError(
            "account_uses_cookie",
            "That account logs in with a pasted li_at cookie - paste a new cookie instead of opening a window.",
            409,
        )
    aid = row.id
    get_runner().submit("connect", lambda: connect_job.execute(linkedin_account_id=aid))
    return session_status(db, aid)


def assert_recheck_allowed(db: Session, account_id: int | None = None) -> None:
    """Raise ``safety_cooldown`` (429) while the account is in its safety cool-down (fixscrape D #4).

    A Re-check loads linkedin.com/feed, which right after a checkpoint / captcha / rate-limit stop is exactly
    the extra hit that makes LinkedIn dig in. Connect stays allowed: it is how the user resolves the warning.
    """
    from sales0.services.runs import cooldown_clock, safety_cooldown_until

    row = get_session_row(db, account_id)
    now = utcnow()
    until = safety_cooldown_until(db, now, row.id)
    if until is None:
        return
    minutes = max(1, -(-int((until - now).total_seconds()) // 60))
    raise DomainError(
        "safety_cooldown",
        f"LinkedIn asked us to slow down - Re-check is paused for {minutes} more minute{'' if minutes == 1 else 's'} "
        f"(until {cooldown_clock(until, now)}). To resolve a LinkedIn warning use Connect LinkedIn instead.",
        429,
    )


def start_check(db: Session, account_id: int | None = None) -> SessionStatusView:
    from sales0.jobs import check_job

    row = get_session_row(db, account_id)
    if row.disabled:
        raise DomainError("account_disabled", "That LinkedIn account is disabled - enable it first.", 409)
    assert_recheck_allowed(db, row.id)
    aid = row.id
    get_runner().submit("check", lambda: check_job.execute(linkedin_account_id=aid))
    return session_status(db, aid)


__all__ = [
    "assert_recheck_allowed",
    "CHROME_MISSING_LABEL",
    "NOT_LOGGED_IN_MESSAGE",
    "browser_channel",
    "browser_info",
    "get_session_row",
    "mark_connected",
    "mark_needs_attention",
    "mark_not_connected",
    "mark_successful_run",
    "note_error",
    "safety_message",
    "session_status",
    "start_check",
    "start_connect",
]
