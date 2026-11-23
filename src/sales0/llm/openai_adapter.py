"""SMA-48: ``LLMClient`` over any OpenAI-compatible Chat Completions endpoint (``POST {base_url}/chat/completions``).

Same contract as the Anthropic adapter: the model must answer by calling the one function (forced
``tool_choice``), own retry/backoff on 429 / 5xx / timeouts, one log line per call, errors mapped to the
``LLMError`` family with user-facing messages that never contain the key, and every outcome noted in
``llm.health`` so the app can say "AI unavailable: <why>".

What the Anthropic-only extras become here: server tools (web search) are not sent - callers check
``supports_web_search`` first; ``effort`` and prompt caching are not sent; a request for the app's stronger model
(research / keyword ideas) uses the admin's "research model" (else the default model).
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from typing import Any

import httpx

from sales0.config import Settings, get_settings
from sales0.llm.client import LLMResponse, StructuredRequest
from sales0.llm.errors import (
    LLMAuthError,
    LLMBadResponse,
    LLMError,
    LLMModelNotFound,
    LLMTransientError,
)
from sales0.llm.provider import ProviderConfig
from sales0.logging import get_logger

log = get_logger("sales0.llm")

BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
PING_PROMPT = "Reply with OK"
PING_MAX_TOKENS = 16  # some reasoning-style models need a few tokens before the word
FIX = "check it on the AI settings page"


def _error_detail(resp: httpx.Response) -> str:
    """The provider's own error sentence (never contains the key), truncated."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text.strip()[:300]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:300]
        if isinstance(error, str):
            return error[:300]
        if isinstance(body.get("message"), str):
            return body["message"][:300]
    return ""


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return "" if content is None else str(content)


def convert_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic-format messages -> Chat Completions messages (text, tool_use -> tool_calls, tool_result -> tool)."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    for msg in messages:
        role, content = msg.get("role"), msg.get("content")
        if not isinstance(content, list):
            out.append({"role": role, "content": _text_of(content)})
            continue
        if role == "assistant":
            calls = [
                {"id": str(b.get("id") or f"call_{i}"), "type": "function",
                 "function": {"name": str(b.get("name") or ""), "arguments": json.dumps(b.get("input") or {})}}
                for i, b in enumerate(content) if isinstance(b, dict) and b.get("type") == "tool_use"
            ]  # fmt: skip
            item: dict[str, Any] = {"role": "assistant", "content": _text_of(content) or None}
            if calls:
                item["tool_calls"] = calls
            out.append(item)
            continue
        text = _text_of(content)
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                out.append({"role": "tool", "tool_call_id": str(block.get("tool_use_id") or ""),
                            "content": _text_of(block.get("content"))})  # fmt: skip
        if text:
            out.append({"role": "user", "content": text})
    return out


def _parse_arguments(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _json_in_text(text: str) -> dict[str, Any] | None:
    """Last resort for endpoints that ignore tools: a JSON object written as the answer (```json fences allowed)."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped[4:] if stripped.lower().startswith("json") else stripped
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    return _parse_arguments(stripped[start : end + 1])


class OpenAICompatibleClient:
    """``LLMClient`` for an OpenAI-compatible endpoint. ``record_health=False`` (the settings page's Test button on
    unsaved values) keeps the outcome out of the app-wide "is the AI working" state."""

    supports_web_search = False

    def __init__(
        self,
        config: ProviderConfig,
        settings: Settings | None = None,
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        record_health: bool = True,
    ) -> None:
        self._cfg = config
        self._settings = settings or get_settings()
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._record = record_health
        if not config.base_url:
            raise LLMBadResponse(f"No endpoint URL is set for the OpenAI-compatible provider - {FIX}")
        if not config.model:
            raise LLMModelNotFound(f"No model is set for the OpenAI-compatible provider - {FIX}")
        if config.key_unreadable:
            raise LLMAuthError("The stored AI provider key cannot be decrypted (SALES0_SECRET_KEY changed) - enter it again on the AI settings page")
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self._http = http or httpx.Client(timeout=self._settings.llm_timeout_s, headers=headers)
        if http is not None:
            self._http.headers.update(headers)

    @property
    def model(self) -> str:
        return self._cfg.model

    def _model_for(self, requested: str | None) -> str:
        s = self._settings
        if requested and requested in {s.research_model, s.keyword_ideas_model} and requested != s.llm_model:
            return self._cfg.research_model or self._cfg.model
        return self._cfg.model

    # ------------------------------------------------------------------ public API
    def structured(self, req: StructuredRequest) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self._model_for(req.model),
            "messages": convert_messages(req.system, req.messages),
            "tools": [{"type": "function", "function": {
                "name": req.tool_name, "description": req.tool_description, "parameters": req.input_schema}}],
            # Server tools are never sent here, so the one function is always forced (``auto`` only existed so the
            # model could search first).
            "tool_choice": {"type": "function", "function": {"name": req.tool_name}},
            "max_tokens": req.max_tokens,
        }  # fmt: skip
        return self._call(body, feature=req.feature, prompt_version=req.prompt_version, tool_name=req.tool_name,
                          log_context=req.log_context)  # fmt: skip

    def ping(self) -> LLMResponse:
        body = {"model": self._cfg.model, "messages": [{"role": "user", "content": PING_PROMPT}],
                "max_tokens": PING_MAX_TOKENS}  # fmt: skip
        return self._call(body, feature="ping", prompt_version="ping-v1", tool_name=None, log_context={})

    # ------------------------------------------------------------------ internals
    def _url(self) -> str:
        base = self._cfg.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    def _backoff_s(self, attempt: int, resp: httpx.Response | None) -> float:
        raw = resp.headers.get("retry-after") if resp is not None else None
        try:
            hinted = float(raw) if raw is not None else None
        except ValueError:
            hinted = None
        if hinted is not None and hinted >= 0:
            return min(hinted, BACKOFF_CAP_S)
        base = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2**attempt))
        return base + self._rng.uniform(0, base / 2)

    def _map_status(self, resp: httpx.Response, model: str) -> LLMError:
        detail = _error_detail(resp)
        suffix = f": {detail}" if detail else ""
        code = resp.status_code
        lowered = detail.lower()
        if code in (401, 403):
            return LLMAuthError(f"The AI provider rejected the key (HTTP {code}) - {FIX}{suffix}")
        if code == 402 or "insufficient_quota" in lowered or ("credit" in lowered and "balance" in lowered):
            return LLMAuthError(f"The AI provider account is out of credits (HTTP {code}){suffix}")
        if code == 404 or ("model" in lowered and ("not found" in lowered or "does not exist" in lowered)):
            return LLMModelNotFound(f"Model '{model}' or the endpoint URL was not found (HTTP {code}) - {FIX}{suffix}")
        if code == 429 or code >= 500:
            return LLMTransientError(f"AI provider unavailable - retry later (HTTP {code}){suffix}")
        return LLMBadResponse(f"The AI provider rejected the request (HTTP {code}){suffix}")

    def _fail(self, mapped: LLMError, *, feature: str, prompt_version: str, log_context: dict[str, Any],
              started: float, attempts: int, cause: BaseException | None = None) -> LLMError:  # fmt: skip
        if self._record:
            from sales0.llm import health

            health.note_failure(mapped.error_type, mapped.message, feature)
        self._log(feature=feature, prompt_version=prompt_version, log_context=log_context, status="error",
                  error_type=mapped.error_type, latency_ms=int((time.monotonic() - started) * 1000), attempts=attempts)  # fmt: skip
        return mapped

    def _call(self, body: dict[str, Any], *, feature: str, prompt_version: str, tool_name: str | None,
              log_context: dict[str, Any]) -> LLMResponse:  # fmt: skip
        max_retries = self._settings.llm_max_retries
        started = time.monotonic()
        attempt = 0
        model = str(body.get("model") or "")
        ctx = dict(feature=feature, prompt_version=prompt_version, log_context=log_context)
        while True:
            resp: httpx.Response | None = None
            try:
                resp = self._http.post(self._url(), json=body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < max_retries:
                    delay = self._backoff_s(attempt, None)
                    log.warning("llm_retry", feature=feature, model=model, attempt=attempt + 1, max_retries=max_retries,
                                error_type=type(exc).__name__, delay_s=round(delay, 2))  # fmt: skip
                    self._sleep(delay)
                    attempt += 1
                    continue
                kind = "timed out" if isinstance(exc, httpx.TimeoutException) else "could not be reached"
                mapped = LLMTransientError(f"The AI provider {kind} ({type(exc).__name__}) - check the endpoint URL or try again later")
                raise self._fail(mapped, started=started, attempts=attempt + 1, **ctx) from exc
            if resp.status_code >= 400:
                transient = resp.status_code == 429 or resp.status_code >= 500
                if transient and attempt < max_retries:
                    delay = self._backoff_s(attempt, resp)
                    log.warning("llm_retry", feature=feature, model=model, attempt=attempt + 1, max_retries=max_retries,
                                error_type=f"HTTP {resp.status_code}", delay_s=round(delay, 2))  # fmt: skip
                    self._sleep(delay)
                    attempt += 1
                    continue
                if (resp.status_code == 400 and isinstance(body.get("tool_choice"), dict)
                        and "tool_choice" in _error_detail(resp).lower()):
                    # Some servers (older vLLM / Ollama builds) only take tool_choice "auto"/"required".
                    log.warning("llm_tool_choice_rejected", feature=feature, model=model)
                    body = {**body, "tool_choice": "required"}
                    continue
                if resp.status_code == 400 and body.get("tool_choice") == "required" and "tool_choice" in _error_detail(resp).lower():
                    body = {k: v for k, v in body.items() if k != "tool_choice"}
                    continue
                raise self._fail(self._map_status(resp, model), started=started, attempts=attempt + 1, **ctx)
            break

        try:
            data = resp.json()
            choice = (data.get("choices") or [None])[0] or {}
            message = choice.get("message") or {}
        except (ValueError, AttributeError, TypeError) as exc:
            mapped = LLMBadResponse("The AI provider sent an answer that is not Chat Completions JSON - check the endpoint URL (it usually ends in /v1)")
            raise self._fail(mapped, started=started, attempts=attempt + 1, **ctx) from exc
        if not isinstance(message, dict):
            message = {}
        tool_input: dict[str, Any] | None = None
        for call in message.get("tool_calls") or []:
            fn = (call or {}).get("function") or {}
            if tool_name is None or fn.get("name") == tool_name:
                tool_input = _parse_arguments(fn.get("arguments"))
                if tool_input is not None:
                    break
        text = _text_of(message.get("content")) or None
        if tool_input is None and tool_name is not None and text:
            tool_input = _json_in_text(text)

        latency_ms = int((time.monotonic() - started) * 1000)
        if self._record:
            from sales0.llm import health

            health.note_success()
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        cost = round(input_tokens * self._cfg.price_input_usd_per_mtok + output_tokens * self._cfg.price_output_usd_per_mtok)
        response = LLMResponse(
            tool_input=tool_input,
            text=text,
            model=str(data.get("model") or model)[:200],
            input_tokens=max(0, input_tokens),
            output_tokens=max(0, output_tokens),
            cost_usd_micros=max(0, cost),
            latency_ms=latency_ms,
            request_id=str(data.get("id"))[:100] if data.get("id") else None,
            stop_reason=choice.get("finish_reason"),
        )
        self._log(feature=feature, prompt_version=prompt_version, log_context=log_context, status="ok", error_type=None,
                  latency_ms=latency_ms, attempts=attempt + 1, response=response)  # fmt: skip
        return response

    def _log(self, *, feature: str, prompt_version: str, log_context: dict[str, Any], status: str,
             error_type: str | None, latency_ms: int, attempts: int, response: LLMResponse | None = None) -> None:  # fmt: skip
        log.info(
            "llm_call",
            provider="openai",
            feature=feature,
            model=response.model if response else self._cfg.model,
            prompt_version=prompt_version,
            input_tokens=response.input_tokens if response else 0,
            output_tokens=response.output_tokens if response else 0,
            cost_usd=(response.cost_usd_micros / 1_000_000) if response else 0.0,
            latency_ms=latency_ms,
            status=status,
            error_type=error_type,
            attempts=attempts,
            input_summary=str(log_context.get("input_summary", ""))[:80],
            stop_reason=response.stop_reason if response else None,
        )


__all__ = ["OpenAICompatibleClient", "convert_messages"]
