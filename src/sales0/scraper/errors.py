"""Scraper errors.

- ``AccountSafetyStop`` aborts the whole run (never retried, never pushed through).
- ``ScrapeError`` fails one keyword (or the run-start open) without touching the account.
- ``SessionMissing`` (Phase 1.1): the profile has no LinkedIn session cookie at all, detected
  before ANY LinkedIn request. Not a safety stop and not a ``ScrapeError``.
- ``BrowserUnavailable`` (Phase 1.1): the requested browser is missing or failed to launch.
"""

from __future__ import annotations

from typing import Literal, get_args

SafetyKind = Literal["logout", "checkpoint", "captcha", "rate_limited"]
SAFETY_KINDS: frozenset[str] = frozenset(get_args(SafetyKind))

_MESSAGES: dict[str, str] = {
    "logout": "LinkedIn session is logged out",
    "checkpoint": "LinkedIn showed a security checkpoint",
    "captcha": "LinkedIn showed a captcha",
    "rate_limited": "LinkedIn rate-limited the session",
}

SESSION_MISSING_MESSAGE = "Not logged in to LinkedIn in sales0's browser — press Connect LinkedIn in Setup"


class AccountSafetyStop(Exception):
    """LinkedIn showed a logout/checkpoint/captcha/rate-limit page. Never push through or retry.

    Deliberately NOT a ``ScrapeError`` subclass, so a per-keyword ``except ScrapeError``
    can never swallow it.
    """

    def __init__(self, kind: SafetyKind, url: str) -> None:
        if kind not in SAFETY_KINDS:
            raise ValueError(f"Unknown safety stop kind: {kind!r}")
        self.kind: SafetyKind = kind
        self.url: str = url
        super().__init__(f"{_MESSAGES[kind]} ({kind}) at {url}")

    @property
    def message(self) -> str:
        return _MESSAGES[self.kind]


class ScrapeError(Exception):
    """A non-safety page failure (navigation error, timeout). The job marks the keyword ``error``."""


class SessionMissing(Exception):
    """No (non-expired) ``li_at`` cookie in the profile; raised before any LinkedIn request.

    Deliberately neither an ``AccountSafetyStop`` nor a ``ScrapeError``: the run ends
    ``failed`` + ``not_connected`` with no cool-down.
    """

    def __init__(self, url: str) -> None:
        self.url: str = url
        super().__init__(f"{SESSION_MISSING_MESSAGE} ({url})")

    @property
    def message(self) -> str:
        return SESSION_MISSING_MESSAGE


class BrowserUnavailable(ScrapeError):
    """Google Chrome was requested but is not installed, or the browser failed to launch."""
