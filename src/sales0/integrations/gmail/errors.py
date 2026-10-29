"""Gmail adapter errors. Every message tells the operator what to do next."""

from __future__ import annotations


class GmailError(Exception):
    """Base class for every Gmail adapter failure."""


class GmailNotConfigured(GmailError):
    """The OAuth client secret or the token file is missing."""


class GmailAuthError(GmailError):
    """The token is revoked, expired or rejected: re-run ``google-auth``."""


class GmailScopeError(GmailAuthError):
    """The stored token lacks a scope this feature needs: re-run ``google-auth``."""


class GmailNotFound(GmailError):
    """The draft or message does not exist (deleted, or already sent)."""


class GmailTransientError(GmailError):
    """429 / 5xx / network failure that survived the retries."""


class GmailApiError(GmailError):
    """Any other non-2xx answer from Google."""


class InvalidMessage(GmailError, ValueError):
    """The message cannot be built safely (bad address, header injection, too large)."""


class ApprovalMismatch(GmailError):
    """The draft no longer matches what was approved. Nothing was sent."""
