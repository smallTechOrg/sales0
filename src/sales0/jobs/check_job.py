"""Re-check job: verify the saved session invisibly (headless), without logging in.

A green Re-check clears ``needs_attention`` but never the safety cool-down (that is
derived from the aborted run, ``services.runs.safety_cooldown_until``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sales0.config import get_settings
from sales0.db.engine import session_scope
from sales0.jobs.session_log import log_session_state
from sales0.logging import get_logger
from sales0.scraper.errors import BrowserUnavailable
from sales0.services import session as session_service

log = get_logger("sales0.jobs.check")

_SAFETY_REASONS = {"checkpoint": "checkpoint", "captcha": "captcha", "rate_limited": "rate_limited"}
CHECK_ERROR_MESSAGE = "Re-check could not load LinkedIn — try again in a minute"


def _default_check() -> Callable[..., Any]:
    from sales0.scraper.session_check import check_session

    return check_session


def execute(check_fn: Callable[..., Any] | None = None) -> None:
    settings = get_settings()
    check = check_fn or _default_check()
    try:
        result = check(settings)  # always headless (Phase 1.1)
    except BrowserUnavailable as exc:
        message = str(exc) or "The browser could not be started — see the server log"
        log.warning("linkedin_session_state", action="check", result="error", reason="browser_unavailable",
                    browser=None, headless=True)
        with session_scope() as db:
            session_service.note_error(db, message)
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("linkedin_session_state", action="check", result="error", reason=type(exc).__name__, headless=True)
        with session_scope() as db:
            session_service.note_error(db, f"Re-check failed ({type(exc).__name__}) — see the server log")
        return

    with session_scope() as db:
        row = session_service.get_session_row(db)
        if result.logged_in:
            session_service.mark_connected(db)
            outcome = "connected"
        elif result.reason in _SAFETY_REASONS:
            session_service.mark_needs_attention(db, _SAFETY_REASONS[result.reason], result.url)
            outcome = "needs_attention"
        elif result.reason == "redirected_login" and row.status == "connected":
            session_service.mark_needs_attention(db, "logout", result.url)
            outcome = "needs_attention"
        elif result.reason in ("no_cookie", "redirected_login"):
            session_service.mark_not_connected(db, session_service.NOT_LOGGED_IN_MESSAGE)
            outcome = "not_connected"
        else:
            session_service.note_error(db, CHECK_ERROR_MESSAGE)
            outcome = "error"
    log_session_state("check", outcome, result.reason, headless=True)
