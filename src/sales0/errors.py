"""Domain errors raised by services and rendered by the web envelope."""

from __future__ import annotations


class DomainError(Exception):
    """A user-facing, expected failure.

    ``code`` is a snake_case identifier listed in ``spec/api.md``; ``message`` is a human
    sentence that tells the user the next step; ``http_status`` is used for JSON responses.
    """

    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status

    def __repr__(self) -> str:
        return f"DomainError(code={self.code!r}, http_status={self.http_status}, message={self.message!r})"
