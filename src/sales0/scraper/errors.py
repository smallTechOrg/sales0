"""Scraper errors. ``AccountSafetyStop`` aborts the whole run; ``ScrapeError`` fails one keyword."""

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
