"""Re-check job: verify the saved session in the background, without logging in."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sales0.config import get_settings
from sales0.db.engine import session_scope
from sales0.logging import get_logger
from sales0.services import session as session_service

log = get_logger("sales0.jobs.check")

_SAFETY_REASONS = {"checkpoint": "checkpoint", "captcha": "captcha", "rate_limited": "rate_limited"}


def _default_check() -> Callable[..., Any]:
    from sales0.scraper.session_check import check_session

    return check_session


def execute(check_fn: Callable[..., Any] | None = None) -> None:
    settings = get_settings()
    check = check_fn or _default_check()
    headless = True if settings.linkedin_mode == "fixture" else settings.linkedin_headless
    try:
        result = check(settings, headless=headless)
    except Exception as exc:  # noqa: BLE001
        log.exception("linkedin_session", action="check", result="error", reason=type(exc).__name__)
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
            session_service.mark_not_connected(db, "Not logged in to LinkedIn — press Connect LinkedIn")
            outcome = "not_connected"
        else:
            session_service.note_error(db, "Re-check could not load LinkedIn — try again in a minute")
            outcome = "error"
    log.info("linkedin_session", action="check", result=outcome, reason=result.reason)
