"""Session log lines emitted by the connect / check / run jobs (architecture.md ->
Conventions -> Logging). Presence booleans only — never a cookie value, never a URL query.

- The scraper (``scraper.session_check.log_session``) emits the detailed
  ``linkedin_session`` line for ``check`` and ``connect`` (status code, verdict, and the
  ``linkedin_dom_hints_missing`` warning). The jobs add ``linkedin_session_state``: what
  the job did to the stored session (``connected|not_connected|needs_attention|error``).
- For ``run_start`` nothing in the scraper logs, so the discovery job emits the
  ``linkedin_session`` line itself from the searcher's ``SessionCheckResult``
  (``linkedin_dom_hints_missing`` still comes from ``verify_with_details``).
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

from sales0.config import get_settings
from sales0.logging import get_logger

log = get_logger("sales0.jobs.session")


def final_path(url: str | None) -> str | None:
    """URL path only (no query, no host) — safe to log."""
    if not url:
        return None
    try:
        return urlsplit(url).path or "/"
    except ValueError:
        return None


def _browser() -> tuple[str | None, str | None]:
    from sales0.scraper import browser as browser_mod

    try:
        choice = browser_mod.resolve_browser(get_settings())
    except Exception:  # noqa: BLE001 — logging must never fail a job
        return None, None
    return choice.channel, choice.version


def log_session_state(action: str, outcome: str, reason: str | None, *, headless: bool,
                      browser: str | None = None) -> None:
    """``linkedin_session_state``: the stored-session outcome of a connect / check job."""
    channel, version = _browser()
    log.info(
        "linkedin_session_state",
        action=action,
        result=outcome,
        reason=reason,
        browser=browser or channel,
        browser_version=version,
        headless=headless,
    )


def log_run_start(outcome: str, reason: str | None, *, url: str | None, cookie_present: bool,
                  soft_hints: Sequence[str] = ()) -> None:
    """The ``linkedin_session`` line for the run-start login check (always headless)."""
    channel, version = _browser()
    hints = list(soft_hints)
    log.info(
        "linkedin_session",
        action="run_start",
        result=outcome,
        reason=reason,
        browser=channel,
        browser_version=version,
        headless=True,
        cookie_present=cookie_present,
        final_path=final_path(url),
        soft_hints=hints,
    )
    # linkedin_dom_hints_missing is logged by scraper.session_check.verify_with_details.


__all__ = ["final_path", "log_run_start", "log_session_state"]
