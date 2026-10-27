"""Connect job: open the visible browser on the dedicated profile and wait for a login."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sales0.config import get_settings
from sales0.db.engine import session_scope
from sales0.jobs.runner import get_runner
from sales0.logging import get_logger
from sales0.services import session as session_service

log = get_logger("sales0.jobs.connect")

FAILURE_MESSAGES: dict[str, str] = {
    "timeout": "Login not completed — try again",
    "window_closed": "The LinkedIn window was closed before the login finished — try again",
    "cancelled": "Connect cancelled",
    "error": "Connect failed — see the server log",
}


def _default_connect() -> Callable[..., Any]:
    from sales0.scraper.connect import connect_interactive

    return connect_interactive


def execute(connect_fn: Callable[..., Any] | None = None) -> None:
    settings = get_settings()
    runner = get_runner()
    connect = connect_fn or _default_connect()
    try:
        result = connect(settings, runner.set_connect_elapsed, runner.connect_cancelled)
    except Exception as exc:  # noqa: BLE001 — reported to the user, logged with the trace
        log.exception("linkedin_session", action="connect", result="error", reason=type(exc).__name__)
        with session_scope() as db:
            session_service.note_error(db, f"Connect failed ({type(exc).__name__}) — see the server log")
        return

    with session_scope() as db:
        if result.connected:
            session_service.mark_connected(db, connected=True)
        else:
            message = FAILURE_MESSAGES.get(result.reason, "Connect failed")
            if result.detail and result.reason == "error":
                message = f"{message}: {result.detail}"
            row = session_service.get_session_row(db)
            if row.status == "needs_attention":
                session_service.note_error(db, message)
            elif result.reason == "cancelled" and row.status == "connected":
                pass  # cancelling an optional re-login keeps the working session
            else:
                session_service.mark_not_connected(db, message)
    log.info(
        "linkedin_session",
        action="connect",
        result="connected" if result.connected else "not_connected",
        reason=result.reason,
    )
