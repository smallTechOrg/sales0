"""LLM abstraction layer: the LLMClient protocol and its Anthropic adapter."""

from __future__ import annotations

import threading

from sales0.llm.client import LLMClient, LLMResponse, StructuredRequest
from sales0.llm.errors import LLMAuthError, LLMBadResponse, LLMError, LLMModelNotFound, LLMTransientError

_lock = threading.Lock()
_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """The process-wide client (AnthropicClient built from settings)."""
    global _client
    with _lock:
        if _client is None:
            from sales0.config import get_settings
            from sales0.llm.anthropic_adapter import AnthropicClient

            _client = AnthropicClient(get_settings())
        return _client


class UnavailableClient:
    """Stands in when no client can be built (e.g. no API key): every call raises that same error, so a route that
    only MIGHT use the AI still runs and the service saves its work without it (and records why)."""

    def __init__(self, error: LLMError) -> None:
        self.error = error

    def structured(self, req: StructuredRequest) -> LLMResponse:
        raise self.error

    def ping(self) -> LLMResponse:
        raise self.error


def client_or_unavailable() -> LLMClient:
    """``get_llm_client()``, or an ``UnavailableClient`` carrying the reason it could not be built."""
    try:
        return get_llm_client()
    except LLMError as exc:
        return UnavailableClient(exc)


def set_llm_client(client: LLMClient | None) -> None:
    """Test seam: install a specific client (e.g. FakeLLMClient)."""
    global _client
    with _lock:
        _client = client


def reset_llm_client() -> None:
    set_llm_client(None)


__all__ = [
    "LLMAuthError",
    "LLMBadResponse",
    "LLMClient",
    "LLMError",
    "LLMModelNotFound",
    "LLMResponse",
    "LLMTransientError",
    "StructuredRequest",
    "UnavailableClient",
    "client_or_unavailable",
    "get_llm_client",
    "reset_llm_client",
    "set_llm_client",
]
