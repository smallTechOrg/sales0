"""SMA-48: which AI provider the app talks to.

The admin picks it on the AI settings page (``ai_provider_config`` row). Without a row, or with provider
'anthropic', it is the server's own Anthropic settings (``SALES0_ANTHROPIC_API_KEY`` / ``SALES0_LLM_MODEL``), as
before. 'openai' is any OpenAI-compatible Chat Completions endpoint: base URL + model + key.

``current()`` re-reads the row at most every ``REFRESH_S`` seconds, so a change saved on one Cloud Run instance
reaches the others (and the worker processes) within that time without a restart. A failed read never breaks
anything: the last known config (else the server settings) is used.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

from sales0.config import Settings, get_settings
from sales0.logging import get_logger

log = get_logger("sales0.llm.provider")

REFRESH_S = 30.0
ProviderName = Literal["anthropic", "openai"]
PROVIDER_LABELS = {"anthropic": "Anthropic", "openai": "OpenAI-compatible"}


@dataclass(frozen=True)
class ProviderConfig:
    provider: ProviderName = "anthropic"
    model: str = ""
    research_model: str = ""  # "" = use ``model``
    base_url: str = ""
    api_key: str = field(default="", repr=False)  # never logged, never rendered
    price_input_usd_per_mtok: float = 0.0
    price_output_usd_per_mtok: float = 0.0
    source: Literal["server", "admin"] = "server"
    key_unreadable: bool = False  # a key is stored but cannot be decrypted (SALES0_SECRET_KEY changed)

    @property
    def key_present(self) -> bool:
        if self.provider == "anthropic":
            return get_settings().api_key_present
        return bool(self.api_key)

    @property
    def label(self) -> str:
        """'Anthropic' or 'OpenAI-compatible (api.groq.com)': how pages name the provider."""
        if self.provider == "anthropic":
            return "Anthropic"
        host = urlparse(self.base_url).hostname or self.base_url
        return f"{PROVIDER_LABELS['openai']} ({host})" if host else PROVIDER_LABELS["openai"]

    @property
    def supports_web_search(self) -> bool:
        """Server-side web search (prospect research) is an Anthropic tool."""
        return self.provider == "anthropic"

    def fingerprint(self) -> tuple[object, ...]:
        return (self.provider, self.model, self.research_model, self.base_url, self.api_key,
                self.price_input_usd_per_mtok, self.price_output_usd_per_mtok)  # fmt: skip


def server_config(settings: Settings | None = None) -> ProviderConfig:
    s = settings or get_settings()
    return ProviderConfig(provider="anthropic", model=s.llm_model, research_model=s.research_model, source="server")


def from_row(row: object | None, settings: Settings | None = None) -> ProviderConfig:
    """The config a stored ``AiProviderConfig`` row stands for (None / 'anthropic' = the server settings)."""
    settings = settings or get_settings()
    if row is None or getattr(row, "provider", "anthropic") != "openai":
        return server_config(settings)
    key, unreadable = "", False
    encrypted = getattr(row, "api_key_encrypted", None)
    if encrypted:
        from sales0.services.team import crypto

        try:
            key = crypto.decrypt(encrypted, settings)
        except crypto.DecryptionError:
            unreadable = True
    model = (getattr(row, "model", None) or "").strip()
    return ProviderConfig(
        provider="openai",
        model=model,
        research_model=(getattr(row, "research_model", None) or "").strip(),
        base_url=(getattr(row, "base_url", None) or "").strip().rstrip("/"),
        api_key=key,
        price_input_usd_per_mtok=float(getattr(row, "price_input_usd_per_mtok", 0) or 0),
        price_output_usd_per_mtok=float(getattr(row, "price_output_usd_per_mtok", 0) or 0),
        source="admin",
        key_unreadable=unreadable,
    )


_lock = threading.Lock()
_cached: ProviderConfig | None = None
_loaded_at = 0.0


def _read_row() -> ProviderConfig:
    from sqlalchemy.orm import Session

    from sales0.db.engine import get_engine
    from sales0.db.models import AiProviderConfig
    from sales0.db.models.ai_provider import AI_PROVIDER_ROW_ID

    with Session(get_engine()) as db:
        return from_row(db.get(AiProviderConfig, AI_PROVIDER_ROW_ID))


def current(*, refresh: bool = False) -> ProviderConfig:
    """The provider in use (cached ``REFRESH_S`` seconds; ``refresh=True`` re-reads now)."""
    global _cached, _loaded_at
    with _lock:
        if not refresh and _cached is not None and time.monotonic() - _loaded_at < REFRESH_S:
            return _cached
    try:
        cfg = _read_row()
    except Exception as exc:  # noqa: BLE001 - no table yet / DB down: keep working on what we know
        log.debug("ai_provider_read_failed", error=type(exc).__name__)
        cfg = _cached or server_config()
    with _lock:
        _cached, _loaded_at = cfg, time.monotonic()
    return cfg


def active_model() -> str:
    """The default model in use: for ``llm_calls`` rows written when a call failed."""
    try:
        return (current().model or get_settings().llm_model)[:100]
    except Exception:  # noqa: BLE001
        return get_settings().llm_model[:100]


def reset() -> None:
    """Forget the cached config (after a save, and in tests)."""
    global _cached, _loaded_at
    with _lock:
        _cached, _loaded_at = None, 0.0


__all__ = ["PROVIDER_LABELS", "ProviderConfig", "active_model", "current", "from_row", "reset", "server_config"]
