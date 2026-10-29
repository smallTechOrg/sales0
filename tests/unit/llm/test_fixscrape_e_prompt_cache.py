"""Prompt caching: cache_control on the system block, cache token pricing, safe fallback."""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx2 as httpx
from pydantic import SecretStr

from sales0.config import Settings
from sales0.llm import StructuredRequest
from sales0.llm.anthropic_adapter import AnthropicClient
from sales0.llm.pricing import cost_usd_micros

SETTINGS = Settings(_env_file=None, anthropic_api_key=SecretStr("not-a-real-key"))


def _msg(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "msg_t", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
        "content": [{"type": "tool_use", "id": "t1", "name": "record_echo", "input": {"word": "hi"}}],
        "stop_reason": "tool_use", "stop_sequence": None, "usage": usage,
    }


def _client(handler_replies: list[httpx.Response]) -> tuple[AnthropicClient, list[dict[str, Any]]]:
    bodies: list[dict[str, Any]] = []
    replies = list(handler_replies)

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return replies.pop(0)

    sdk = anthropic.Anthropic(
        api_key="k", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    return AnthropicClient(SETTINGS, sdk=sdk), bodies


def _req(system: str = "stable system prompt") -> StructuredRequest:
    return StructuredRequest(
        feature="triage", system=system, messages=[{"role": "user", "content": "hi"}],
        tool_name="record_echo", tool_description="Echo",
        input_schema={"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]},
        max_tokens=50, prompt_version="t-v1",
    )


def test_system_is_one_cacheable_text_block() -> None:
    client, bodies = _client([httpx.Response(200, json=_msg({"input_tokens": 10, "output_tokens": 5}))])
    client.structured(_req())
    assert bodies[0]["system"] == [
        {"type": "text", "text": "stable system prompt", "cache_control": {"type": "ephemeral"}}
    ]


def test_usage_without_cache_fields_defaults_to_zero_and_old_cost() -> None:
    client, _ = _client([httpx.Response(200, json=_msg({"input_tokens": 1000, "output_tokens": 100}))])
    r = client.structured(_req())
    assert (r.cache_creation_input_tokens, r.cache_read_input_tokens) == (0, 0)
    assert r.cost_usd_micros == cost_usd_micros(1000, 100, SETTINGS)


def test_null_cache_fields_are_zero() -> None:
    usage = {"input_tokens": 5, "output_tokens": 1, "cache_creation_input_tokens": None, "cache_read_input_tokens": None}
    client, _ = _client([httpx.Response(200, json=_msg(usage))])
    r = client.structured(_req())
    assert (r.cache_creation_input_tokens, r.cache_read_input_tokens) == (0, 0)


def test_cache_write_and_read_tokens_are_priced() -> None:
    usage = {"input_tokens": 100, "output_tokens": 10, "cache_creation_input_tokens": 4000, "cache_read_input_tokens": 0}
    client, _ = _client([httpx.Response(200, json=_msg(usage))])
    w = client.structured(_req())
    pin, pout = SETTINGS.llm_price_input_usd_per_mtok, SETTINGS.llm_price_output_usd_per_mtok
    assert w.cache_creation_input_tokens == 4000
    assert w.cost_usd_micros == round(100 * pin + 4000 * pin * 1.25 + 10 * pout)

    usage = {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 4000}  # partial: no creation key
    client, _ = _client([httpx.Response(200, json=_msg(usage))])
    r = client.structured(_req())
    assert (r.cache_creation_input_tokens, r.cache_read_input_tokens) == (0, 4000)
    assert r.cost_usd_micros == round(100 * pin + 4000 * pin * 0.1 + 10 * pout)
    assert r.cost_usd_micros < w.cost_usd_micros


def test_cache_control_rejection_falls_back_to_plain_system_once() -> None:
    err = httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "cache_control: not supported"}})
    ok = httpx.Response(200, json=_msg({"input_tokens": 10, "output_tokens": 5}))
    client, bodies = _client([err, ok])
    r = client.structured(_req())
    assert r.tool_input == {"word": "hi"}
    assert isinstance(bodies[0]["system"], list)
    assert bodies[1]["system"] == "stable system prompt"


def test_other_400_is_not_swallowed() -> None:
    import pytest

    from sales0.llm import LLMBadResponse

    err = httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}})
    client, bodies = _client([err])
    with pytest.raises(LLMBadResponse):
        client.structured(_req())
    assert len(bodies) == 1


def test_ping_has_no_system_block() -> None:
    client, bodies = _client([httpx.Response(200, json=_msg({"input_tokens": 3, "output_tokens": 1}))])
    client.ping()
    assert "system" not in bodies[0]
