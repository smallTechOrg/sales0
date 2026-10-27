"""Connect job: open sales0's Google Chrome window (headed in live mode) on the dedicated
profile and wait for the user to log in (incl. 2FA). A successful Connect clears
``needs_attention`` but never the safety cool-down."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sales0.config import get_settings
from sales0.db.engine import session_scope
from sales0.jobs.runner import get_runner
from sales0.jobs.session_log import log_session_state
from sales0.logging import get_logger
from sales0.scraper.errors import BrowserUnavailable
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


def _failure_message(reason: str, detail: str | None) -> str:
    if reason == "error" and detail:
        # connect_interactive's error details are user-facing sentences with a next step
        # (BrowserUnavailable message, "LinkedIn is rate-limiting this browser — …").
        return detail
    return FAILURE_MESSAGES.get(reason, "Connect failed")


def _record_failure(reason: str, message: str) -> None:
    with session_scope() as db:
        row = session_service.get_session_row(db)
        if row.status == "needs_attention":
            session_service.note_error(db, message)  # keeps the stop reason + banner
        elif row.status == "connected" and reason == "cancelled":
            pass  # cancelling an optional re-login keeps the working session
        elif row.status == "connected" and reason == "error":
            # The browser could not open / LinkedIn refused: the saved login is still there.
            session_service.note_error(db, message)
        else:
            session_service.mark_not_connected(db, message)


def execute(connect_fn: Callable[..., Any] | None = None) -> None:
    settings = get_settings()
    runner = get_runner()
    connect = connect_fn or _default_connect()
    headless = settings.linkedin_mode == "fixture"
    try:
        result = connect(settings, runner.set_connect_elapsed, runner.connect_cancelled)
    except BrowserUnavailable as exc:
        message = str(exc) or "The browser could not be started — see the server log"
        log.warning("linkedin_session_state", action="connect", result="error", reason="browser_unavailable",
                    browser=None, headless=headless)
        _record_failure("error", message)
        return
    except Exception as exc:  # noqa: BLE001 — reported to the user, logged with the trace
        log.exception("linkedin_session_state", action="connect", result="error", reason=type(exc).__name__,
                      headless=headless)
        with session_scope() as db:
            session_service.note_error(db, f"Connect failed ({type(exc).__name__}) — see the server log")
        return

    if result.connected:
        with session_scope() as db:
            session_service.mark_connected(db, connected=True)
    else:
        _record_failure(result.reason, _failure_message(result.reason, result.detail))
    log_session_state(
        "connect",
        "connected" if result.connected else "not_connected",
        result.reason,
        headless=headless,
        browser=getattr(result, "browser", None),
    )
