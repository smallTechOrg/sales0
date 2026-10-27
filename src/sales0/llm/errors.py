"""LLM layer errors. Messages are user-facing and never contain secret values."""

from __future__ import annotations


class LLMError(Exception):
    """Base class. ``error_type`` is the short label used in logs and llm_calls rows."""

    error_type = "llm_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class LLMAuthError(LLMError):
    """Anthropic rejected the key (401/403) or the key is missing. Fatal."""

    error_type = "auth"


class LLMModelNotFound(LLMError):
    """The configured model id does not exist for this account (404). Fatal."""

    error_type = "model_not_found"


class LLMTransientError(LLMError):
    """429 / 5xx / timeout / connection failure that survived every retry."""

    error_type = "transient"


class LLMBadResponse(LLMError):
    """The request was rejected as invalid (4xx) or the response could not be used."""

    error_type = "bad_response"
