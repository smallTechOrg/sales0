"""LinkedIn session status (singleton row) + connect / re-check job starts."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.models import LinkedInSession
from sales0.jobs.runner import get_runner
from sales0.services._common import clip, now_or, record_history
from sales0.services.views import SessionStatusView

SESSION_ID = 1

_SAFETY_TEMPLATES: dict[str, str] = {
    "logout": "Logged out of LinkedIn (login page at {path}) — Connect LinkedIn again",
    "checkpoint": "Checkpoint page at {path} — open LinkedIn manually, resolve it, then Connect again",
    "captcha": "Captcha shown at {path} — open LinkedIn manually, solve it, then Connect again",
    "rate_limited": "LinkedIn rate-limited the session at {path} — wait a few hours, then Re-check",
}


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


def session_status(db: Session) -> SessionStatusView:
    row = get_session_row(db)
    runner = get_runner()
    busy = runner.busy()
    stored = row.status if row.status in ("not_connected", "connected", "needs_attention") else "not_connected"
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
