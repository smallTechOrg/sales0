"""Anthropic adapter: forced tool use, own retry/backoff, usage + cost, one log line per call.

The adapter never touches the database. Callers persist ``llm_calls`` rows from the
returned ``LLMResponse``.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import anthropic

from sales0.config import Settings
from sales0.llm.client import LLMResponse, StructuredRequest
from sales0.llm.errors import (
    LLMAuthError,
    LLMBadResponse,
    LLMError,
    LLMModelNotFound,
    LLMTransientError,
    LLMWorkspaceRequired,
)
from sales0.llm.pricing import cost_usd_micros, micros_to_usd
from sales0.logging import get_logger

log = get_logger("sales0.llm")

# Keys structlog / the renderer own on every line; caller context may not overwrite them.
_RESERVED_LOG_KEYS = frozenset({"event", "timestamp", "level", "logger", "exc_info", "stack_info"})

BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
CACHE_CONTROL = {"type": "ephemeral"}
PING_PROMPT = "Reply with OK"
PING_MAX_TOKENS = 5

AUTH_MESSAGE = "Anthropic rejected the key — check SALES0_ANTHROPIC_API_KEY in .env"
MISSING_KEY_MESSAGE = "SALES0_ANTHROPIC_API_KEY is missing — add it to .env in the repo root"
WORKSPACE_MESSAGE = "This Anthropic key is not workspace-scoped — set SALES0_ANTHROPIC_WORKSPACE_ID in .env"


def _api_error_message(exc: Exception) -> str:
    """The provider's own error sentence (never contains the key), truncated."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:300]
    return ""


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, anthropic.RateLimitError | anthropic.APIConnectionError):  # APITimeoutError ⊂ APIConnectionError
        return True
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500


def _retry_after_s(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after")
    try:
        value = float(raw) if raw is not None else None
    except ValueError:
        return None
    if value is None or value < 0:
        return None
    return min(value, BACKOFF_CAP_S)


def _system_blocks(system: str) -> str | list[dict[str, Any]]:
    """The system prompt as one cacheable text block (prompt caching, 5-min ephemeral).

    Below the model's minimum cacheable length the API silently ignores the marker, so
    this is always safe to send. An empty system stays a plain string.
    """
    if not system:
        return system
    return [{"type": "text", "text": system, "cache_control": dict(CACHE_CONTROL)}]


def _plain_system(params: dict[str, Any]) -> dict[str, Any]:
    system = params.get("system")
    if isinstance(system, list):
        return {**params, "system": "".join(str(b.get("text", "")) for b in system)}
    return params


def _int_field(usage: Any, name: str) -> int:
    value = getattr(usage, name, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _summarise_output(tool_input: dict[str, Any] | None, text: str | None) -> str:
    if tool_input is not None:
        picked = {k: tool_input[k] for k in ("verdict", "priority", "offering_id") if k in tool_input}
        if picked:
            return " ".join(f"{k}={v}" for k, v in picked.items())
        return "tool_input keys=" + ",".join(sorted(tool_input))[:80]
    if text:
        return "text=" + text.strip()[:40]
    return "empty"


class AnthropicClient:
    """``LLMClient`` implementation over the Anthropic Messages API."""

    def __init__(
        self,
        settings: Settings,
        *,
        sdk: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._rng = rng or random.Random()
        if sdk is not None:
            self._sdk = sdk
        else:
            if not settings.api_key_present:
                raise LLMAuthError(MISSING_KEY_MESSAGE)
            assert settings.anthropic_api_key is not None
            headers = (
                {"anthropic-workspace-id": settings.anthropic_workspace_id} if settings.anthropic_workspace_id else None
            )
            self._sdk = anthropic.Anthropic(
                api_key=settings.anthropic_api_key.get_secret_value(),
                max_retries=0,  # the adapter owns retries
                timeout=settings.llm_timeout_s,
                default_headers=headers,
            )

    @property
    def model(self) -> str:
        return self._settings.llm_model

    # ------------------------------------------------------------------ public API
    def structured(self, req: StructuredRequest) -> LLMResponse:
        params: dict[str, Any] = {
            "model": self._settings.llm_model,
            "system": _system_blocks(req.system),
            "messages": req.messages,
            "tools": [
                {
                    "name": req.tool_name,
                    "description": req.tool_description,
                    "input_schema": req.input_schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": req.tool_name},
            "max_tokens": req.max_tokens,
        }
        # Never send temperature / top_p / top_k / thinking: claude-haiku-5-5 rejects them (400).
        if req.effort is not None:
            params["output_config"] = {"effort": req.effort}
        return self._call(
            params,
            feature=req.feature,
            prompt_version=req.prompt_version,
            tool_name=req.tool_name,
            log_context=req.log_context,
        )

    def ping(self) -> LLMResponse:
        params: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": [{"role": "user", "content": PING_PROMPT}],
            "max_tokens": PING_MAX_TOKENS,
        }
        return self._call(params, feature="ping", prompt_version="ping-v1", tool_name=None, log_context={})

    # ------------------------------------------------------------------ internals
    def _backoff_s(self, attempt: int, exc: Exception) -> float:
        hinted = _retry_after_s(exc)
        if hinted is not None:
            return hinted
        base = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2**attempt))
        return base + self._rng.uniform(0, base / 2)

    def _map_error(self, exc: Exception) -> LLMError:
        if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
            return LLMAuthError(AUTH_MESSAGE)
        if isinstance(exc, anthropic.NotFoundError):
            return LLMModelNotFound(f"Model '{self._settings.llm_model}' not found — check SALES0_LLM_MODEL")
        if _is_transient(exc):
            return LLMTransientError(f"LLM unavailable — retry later ({type(exc).__name__})")
        if isinstance(exc, anthropic.APIStatusError):
            detail = _api_error_message(exc)
            if exc.status_code == 400 and "anthropic-workspace-id" in detail:
                return LLMWorkspaceRequired(WORKSPACE_MESSAGE)
            suffix = f": {detail}" if detail else ""
            return LLMBadResponse(f"Anthropic rejected the request (HTTP {exc.status_code}, {type(exc).__name__}){suffix}")
        return LLMBadResponse(f"Unexpected LLM failure ({type(exc).__name__})")

    def _call(
        self,
        params: dict[str, Any],
        *,
        feature: str,
        prompt_version: str,
        tool_name: str | None,
        log_context: dict[str, Any],
    ) -> LLMResponse:
        max_retries = self._settings.llm_max_retries
        started = time.monotonic()
        attempt = 0
        while True:
            try:
                message = self._sdk.messages.create(**params)
                break
            except Exception as exc:  # noqa: BLE001 — mapped below; nothing is swallowed
                if _is_transient(exc) and attempt < max_retries:
                    delay = self._backoff_s(attempt, exc)
                    log.warning(
                        "llm_retry",
                        feature=feature,
                        model=self._settings.llm_model,
                        attempt=attempt + 1,
                        max_retries=max_retries,
                        error_type=type(exc).__name__,
                        delay_s=round(delay, 2),
                    )
                    self._sleep(delay)
                    attempt += 1
                    continue
                if (
                    isinstance(params.get("system"), list)
                    and isinstance(exc, anthropic.APIStatusError)
                    and exc.status_code == 400
                    and "cache_control" in _api_error_message(exc)
                ):
                    # The API rejected prompt caching: fall back to the plain string system.
                    log.warning("llm_cache_control_rejected", feature=feature, model=self._settings.llm_model)
                    params = _plain_system(params)
                    continue
                mapped = self._map_error(exc)
                self._log_call(
                    feature=feature,
                    prompt_version=prompt_version,
                    log_context=log_context,
                    status="error",
                    error_type=mapped.error_type,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    attempts=attempt + 1,
                )
                raise mapped from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        tool_input: dict[str, Any] | None = None
        texts: list[str] = []
        for block in getattr(message, "content", None) or []:
            block_type = getattr(block, "type", None)
            if block_type == "tool_use" and (tool_name is None or getattr(block, "name", None) == tool_name):
                raw = getattr(block, "input", None)
                if isinstance(raw, dict) and tool_input is None:
                    tool_input = dict(raw)
            elif block_type == "text":
                texts.append(getattr(block, "text", "") or "")

        usage = getattr(message, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_write = _int_field(usage, "cache_creation_input_tokens")
        cache_read = _int_field(usage, "cache_read_input_tokens")
        cost = cost_usd_micros(input_tokens, output_tokens, self._settings, cache_write, cache_read)
        text = "".join(texts) or None
        response = LLMResponse(
            tool_input=tool_input,
            text=text,
            model=str(getattr(message, "model", None) or self._settings.llm_model),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd_micros=cost,
            cache_creation_input_tokens=cache_write,
            cache_read_input_tokens=cache_read,
            latency_ms=latency_ms,
            request_id=getattr(message, "_request_id", None) or getattr(message, "id", None),
            stop_reason=getattr(message, "stop_reason", None),
        )
        self._log_call(
            feature=feature,
            prompt_version=prompt_version,
            log_context=log_context,
            status="ok",
            error_type=None,
            latency_ms=latency_ms,
            attempts=attempt + 1,
            response=response,
        )
        return response

    def _log_call(
        self,
        *,
        feature: str,
        prompt_version: str,
        log_context: dict[str, Any],
        status: str,
        error_type: str | None,
        latency_ms: int,
        attempts: int,
        response: LLMResponse | None = None,
    ) -> None:
        fields: dict[str, Any] = dict(
            feature=feature,
            model=self._settings.llm_model,
            prompt_version=prompt_version,
            input_tokens=response.input_tokens if response else 0,
            output_tokens=response.output_tokens if response else 0,
            cache_creation_input_tokens=response.cache_creation_input_tokens if response else 0,
            cache_read_input_tokens=response.cache_read_input_tokens if response else 0,
            cost_usd=micros_to_usd(response.cost_usd_micros) if response else 0.0,
            latency_ms=latency_ms,
            status=status,
            error_type=error_type,
            fewshot_count=log_context.get("fewshot_count", 0),
            input_summary=str(log_context.get("input_summary", ""))[:80],
            output_summary=_summarise_output(response.tool_input, response.text) if response else None,
            attempts=attempts,
            stop_reason=response.stop_reason if response else None,
        )
        # Caller context must never break logging after a (paid) call: keys that collide
        # with a fixed field or a structlog-reserved key are kept under a ``ctx_`` prefix.
        reserved = set(fields) | _RESERVED_LOG_KEYS
        for key, value in log_context.items():
            if key in {"fewshot_count", "input_summary"}:
                continue
            name = str(key)
            while name in reserved or name in fields:
                name = f"ctx_{name}"
            fields[name] = value
        log.info("llm_call", **fields)
