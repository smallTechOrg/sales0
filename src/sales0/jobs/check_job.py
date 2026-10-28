"""Re-check job: verify the saved session invisibly (headless), without logging in.

A green Re-check clears ``needs_attention`` but never the safety cool-down (that is
derived from the aborted run AND from the account's ``last_stop_at``, ``services.runs.safety_cooldown_until``).
A stop the Re-check itself detects (checkpoint / captcha / rate limit / logout) stamps the account's
``safety_state`` / ``last_stop_at`` and starts that cool-down, during which Re-check is refused
(``services.session.assert_recheck_allowed``) and only Connect is allowed.
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


def _default_check(account_id: int) -> Callable[..., Any]:
    from sales0.scraper.session_check import check_session
    from sales0.services import linkedin_accounts

    def check(settings: Any) -> Any:
        return check_session(settings, linkedin_accounts.build_handle(account_id))

    return check


def _snapshot_cooldown(db: Any, account_id: int) -> None:
    """Mirror the cool-down this stop started onto the account row (display / audit)."""
    from sales0.services import runs as runs_service

    runs_service.sync_account_state(db, account_id)


def execute(check_fn: Callable[..., Any] | None = None, *, linkedin_account_id: int | None = None) -> None:
    """Re-check ONE LinkedIn account (default: the first active one)."""
    settings = get_settings()
    with session_scope() as db:
        account_id = session_service.get_session_row(db, linkedin_account_id).id
    check = check_fn or _default_check(account_id)
    try:
        result = check(settings)  # always headless (Phase 1.1)
    except BrowserUnavailable as exc:
        message = str(exc) or "The browser could not be started — see the server log"
        log.warning("linkedin_session_state", action="check", result="error", reason="browser_unavailable",
                    browser=None, headless=True)
        with session_scope() as db:
            session_service.note_error(db, message, account_id=account_id)
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("linkedin_session_state", action="check", result="error", reason=type(exc).__name__, headless=True)
        with session_scope() as db:
            session_service.note_error(db, f"Re-check failed ({type(exc).__name__}) — see the server log", account_id=account_id)
        return

    with session_scope() as db:
        row = session_service.get_session_row(db, account_id)
        if result.logged_in:
            session_service.mark_connected(db, account_id=account_id)
            outcome = "connected"
        elif result.reason in _SAFETY_REASONS:
            session_service.mark_needs_attention(db, _SAFETY_REASONS[result.reason], result.url, account_id=account_id)
            _snapshot_cooldown(db, account_id)
            outcome = "needs_attention"
        elif result.reason == "redirected_login" and row.status == "connected":
            session_service.mark_needs_attention(db, "logout", result.url, account_id=account_id)
            _snapshot_cooldown(db, account_id)
            outcome = "needs_attention"
        elif result.reason in ("no_cookie", "redirected_login"):
            session_service.mark_not_connected(db, session_service.NOT_LOGGED_IN_MESSAGE, account_id=account_id)
            if row.backend == "db":
                # a pasted cookie LinkedIn does not accept is not kept
                from sales0.services import linkedin_accounts

                linkedin_accounts.clear_session_state(db, account_id)
            outcome = "not_connected"
        else:
            session_service.note_error(db, CHECK_ERROR_MESSAGE, account_id=account_id)
            outcome = "error"
    log_session_state("check", outcome, result.reason, headless=True)
