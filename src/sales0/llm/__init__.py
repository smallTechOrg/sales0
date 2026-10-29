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
    "get_llm_client",
    "reset_llm_client",
    "set_llm_client",
]
