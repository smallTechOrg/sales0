"""LinkedIn session status (singleton row) + connect / re-check job starts."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.models import LinkedInSession
from sales0.jobs.runner import get_runner
from sales0.logging import get_logger
from sales0.services._common import clip, now_or, record_history
from sales0.services.views import SessionStatusView

SESSION_ID = 1

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


def get_session_row(db: Session) -> LinkedInSession:
    row = db.get(LinkedInSession, SESSION_ID)
    if row is None:  # seeded by migration 0002; recreate defensively
        row = LinkedInSession(id=SESSION_ID, status="not_connected")
        db.add(row)
        db.flush()
    return row


def _set_status(db: Session, row: LinkedInSession, status: str, *, note: str | None, now: datetime) -> None:
    if row.status != status:
        record_history(db, "linkedin_session", row.id, "status", row.status, status, note=note, now=now)
        row.status = status
    row.updated_at = now


def mark_connected(db: Session, *, now: datetime | None = None, connected: bool = False) -> None:
    now = now_or(now)
    row = get_session_row(db)
    _set_status(db, row, "connected", note="connect" if connected else "check", now=now)
    row.last_checked_at = now
    if connected:
        row.last_connected_at = now
    row.last_error = None
    db.flush()


def mark_not_connected(db: Session, message: str | None, *, now: datetime | None = None) -> None:
    now = now_or(now)
    row = get_session_row(db)
    _set_status(db, row, "not_connected", note=message, now=now)
    row.last_checked_at = now
    row.last_error = clip(message, 300)
    db.flush()


def mark_needs_attention(db: Session, kind: str, url: str | None, *, now: datetime | None = None) -> str:
    now = now_or(now)
    message = safety_message(kind, url)
    row = get_session_row(db)
    _set_status(db, row, "needs_attention", note=kind, now=now)
    row.last_checked_at = now
    row.last_error = message
    db.flush()
    return message


def note_error(db: Session, message: str, *, now: datetime | None = None) -> None:
    """Record a non-state-changing error (e.g. the check itself crashed)."""
    now = now_or(now)
    row = get_session_row(db)
    row.last_checked_at = now
    row.last_error = clip(message, 300)
    row.updated_at = now
    db.flush()


def mark_successful_run(db: Session, *, now: datetime | None = None) -> None:
    row = get_session_row(db)
    row.last_successful_run_at = now_or(now)
    db.flush()


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


def session_status(db: Session) -> SessionStatusView:
    from sales0.services.runs import safety_cooldown_until

    row = get_session_row(db)
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
        cooldown_until=safety_cooldown_until(db),
    )


def start_connect(db: Session) -> SessionStatusView:
    from sales0.jobs import connect_job

    get_runner().submit("connect", connect_job.execute)
    return session_status(db)


def start_check(db: Session) -> SessionStatusView:
    from sales0.jobs import check_job

    get_runner().submit("check", check_job.execute)
    return session_status(db)


__all__ = [
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
