"""The wire shape of every Haiku 5.5 request.

Since 2026-10-21 claude-haiku-5-5 rejects (HTTP 400) any sampling or extended-thinking
parameter: temperature, top_p, top_k, thinking / budget_tokens. These tests drive the
real ``anthropic`` SDK over an ``httpx.MockTransport`` and inspect the exact JSON body the
adapter would put on the wire, so a stray kwarg anywhere in the stack is caught.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx2 as httpx
import pytest
from pydantic import SecretStr, ValidationError

from sales0.config import Settings
from sales0.llm import StructuredRequest
from sales0.llm.anthropic_adapter import AnthropicClient

FORBIDDEN_KEYS = frozenset({"temperature", "top_p", "top_k", "thinking", "budget_tokens"})


def _all_keys(value: Any) -> set[str]:
    """Every dict key at any depth of a decoded JSON body."""
    if isinstance(value, dict):
        keys = set(value)
        for child in value.values():
            keys |= _all_keys(child)
        return keys
    if isinstance(value, list):
        return set().union(*(_all_keys(v) for v in value)) if value else set()
    return set()


def _tool_message() -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "record_echo", "input": {"word": "hi"}}],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def _text_message() -> dict[str, Any]:
    return {**_tool_message(), "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn"}


def _client(reply: dict[str, Any]) -> tuple[AnthropicClient, list[dict[str, Any]]]:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=reply)

    sdk = anthropic.Anthropic(
        api_key="not-a-real-key", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    settings = Settings(_env_file=None, anthropic_api_key=SecretStr("not-a-real-key"))
    return AnthropicClient(settings, sdk=sdk), bodies


def _request(**overrides: Any) -> StructuredRequest:
    values: dict[str, Any] = dict(
        feature="triage",
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        tool_name="record_echo",
        tool_description="Echo",
        input_schema={"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]},
        max_tokens=50,
        prompt_version="t-v1",
    )
    return StructuredRequest(**{**values, **overrides})


def test_structured_body_has_no_sampling_or_thinking_params() -> None:
    client, bodies = _client(_tool_message())
    resp = client.structured(_request())
    assert resp.tool_input == {"word": "hi"}
    (body,) = bodies
    assert set(body) == {"model", "system", "messages", "tools", "tool_choice", "max_tokens"}
    assert body["model"] == "claude-haiku-5-5"
    assert body["tool_choice"] == {"type": "tool", "name": "record_echo"}
    assert not (_all_keys(body) - {"word"}) & FORBIDDEN_KEYS


def test_ping_body_has_no_sampling_or_thinking_params() -> None:
    client, bodies = _client(_text_message())
    assert client.ping().text == "OK"
    (body,) = bodies
    assert set(body) == {"model", "messages", "max_tokens"}
    assert not _all_keys(body) & FORBIDDEN_KEYS


def test_effort_defaults_to_not_sent() -> None:
    client, bodies = _client(_tool_message())
    client.structured(_request())
    assert "output_config" not in bodies[0]


def test_effort_low_is_sent_as_output_config_alongside_forced_tool_choice() -> None:
    client, bodies = _client(_tool_message())
    client.structured(_request(effort="low"))
    (body,) = bodies
    assert body["output_config"] == {"effort": "low"}
    assert body["tool_choice"] == {"type": "tool", "name": "record_echo"}
    assert not _all_keys(body) & FORBIDDEN_KEYS


@pytest.mark.parametrize("bad", ["minimal", "", "LOW", 3])
def test_effort_rejects_values_outside_the_allowed_set(bad: Any) -> None:
    with pytest.raises(ValidationError):
        _request(effort=bad)


def test_research_model_override_is_sent_and_priced_with_the_research_price() -> None:
    client, bodies = _client({**_tool_message(), "model": "claude-sonnet-5-5"})
    base = client.structured(_request())
    over = client.structured(_request(model="claude-sonnet-5-5"))
    assert bodies[0]["model"] == "claude-haiku-5-5" and bodies[1]["model"] == "claude-sonnet-5-5"
    s = Settings(_env_file=None)
    assert base.cost_usd_micros == round(100 * s.llm_price_input_usd_per_mtok + 20 * s.llm_price_output_usd_per_mtok)
    assert over.cost_usd_micros == round(100 * s.research_price_input_usd_per_mtok + 20 * s.research_price_output_usd_per_mtok)
    assert over.cost_usd_micros > base.cost_usd_micros
