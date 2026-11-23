"""LLM abstraction layer: the LLMClient protocol and its adapters (Anthropic; any OpenAI-compatible endpoint)."""

from __future__ import annotations

import threading

from sales0.llm.client import LLMClient, LLMResponse, StructuredRequest
from sales0.llm.errors import LLMAuthError, LLMBadResponse, LLMError, LLMModelNotFound, LLMTransientError

_lock = threading.Lock()
_client: LLMClient | None = None  # installed by a test (``set_llm_client``): always wins
_built: tuple[tuple[object, ...], LLMClient] | None = None  # (provider fingerprint, the client built for it)


def build_client(config, *, record_health: bool = True) -> LLMClient:  # type: ignore[no-untyped-def]
    """A client for ``config`` (an ``llm.provider.ProviderConfig``). Raises ``LLMError`` when it cannot be built."""
    from sales0.config import get_settings

    if config.provider == "openai":
        from sales0.llm.openai_adapter import OpenAICompatibleClient

        return OpenAICompatibleClient(config, get_settings(), record_health=record_health)
    from sales0.llm.anthropic_adapter import AnthropicClient

    if not record_health:
        return AnthropicClient(get_settings(), record_health=False)
    return AnthropicClient(get_settings())


def get_llm_client() -> LLMClient:
    """The process-wide client for the provider in use (SMA-48: the admin's choice on the AI settings page, else
    Anthropic from the server settings). Rebuilt when that choice changes."""
    global _built
    with _lock:
        if _client is not None:
            return _client
    from sales0.llm import provider

    config = provider.current()
    key = config.fingerprint()
    with _lock:
        if _built is not None and _built[0] == key:
            return _built[1]
    client = build_client(config)
    with _lock:
        _built = (key, client)
    return client


def supports_web_search(client: object | None = None) -> bool:
    """Can ``client`` (default: the one in use) run Anthropic's server-side web search? Clients that do not say
    (test doubles) are treated as able to, as before."""
    if client is None:
        with _lock:
            client = _client
    if client is not None:
        return bool(getattr(client, "supports_web_search", True))
    from sales0.llm import provider

    return provider.current().supports_web_search


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
    """Test seam: install a specific client (e.g. FakeLLMClient); ``None`` goes back to the configured provider."""
    global _client, _built
    with _lock:
        _client = client
        _built = None


def reset_llm_client() -> None:
    """Drop the installed / built client and the cached provider choice (e.g. after the admin saves a new one)."""
    from sales0.llm import provider

    set_llm_client(None)
    provider.reset()


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
    "build_client",
    "client_or_unavailable",
    "get_llm_client",
    "reset_llm_client",
    "set_llm_client",
    "supports_web_search",
]
