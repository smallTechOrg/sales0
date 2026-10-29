"""Server-side web search on the research call: wire shape (real SDK over a mock transport), usage + cost, results."""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx2 as httpx
import pytest
from pydantic import SecretStr

from sales0.config import Settings
from sales0.llm import StructuredRequest
from sales0.llm.anthropic_adapter import AnthropicClient
from sales0.llm.pricing import cost_usd_micros, web_search_cost_usd_micros

WEB_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}


def _reply(*, searches: int = 2, error_block: bool = False) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "acme news"}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
            {"type": "web_search_result", "url": "https://news.example/acme", "title": "Acme expands", "encrypted_content": "x", "page_age": "2 days ago"},
            {"type": "web_search_result", "url": "javascript:alert(1)", "title": "bad", "encrypted_content": "y"},
        ]},
    ]  # fmt: skip
    if error_block:
        content.append({"type": "web_search_tool_result", "tool_use_id": "srvtoolu_2",
                        "content": {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"}})
    content.append({"type": "tool_use", "id": "toolu_1", "name": "record_web_findings",
                    "input": {"summary": "s", "findings": [{"fact": "f", "url": "https://news.example/acme"}]}})
    return {
        "id": "msg_ws", "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "content": content,
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 5000, "output_tokens": 300, "server_tool_use": {"web_search_requests": searches}},
    }  # fmt: skip


def _client(reply: dict[str, Any]) -> tuple[AnthropicClient, list[dict[str, Any]], Settings]:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=reply)

    sdk = anthropic.Anthropic(api_key="k", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    settings = Settings(_env_file=None, anthropic_api_key=SecretStr("k"))
    return AnthropicClient(settings, sdk=sdk), bodies, settings


def _req(**kw: Any) -> StructuredRequest:
    base = dict(
        feature="research", system="sys", messages=[{"role": "user", "content": "hi"}], tool_name="record_web_findings",
        tool_description="d", input_schema={"type": "object", "properties": {}}, max_tokens=100, prompt_version="t",
        server_tools=[WEB_TOOL], tool_choice="auto", cache_system=False,
    )  # fmt: skip
    base.update(kw)
    return StructuredRequest(**base)


def test_server_tool_is_sent_first_with_auto_tool_choice_and_uncached_system() -> None:
    client, bodies, _ = _client(_reply())
    client.structured(_req())
    body = bodies[0]
    assert body["tools"][0] == WEB_TOOL and body["tools"][1]["name"] == "record_web_findings"
    assert body["tool_choice"] == {"type": "auto"}
    assert body["system"] == "sys"  # plain string: no cache write over the search results
    assert not {"temperature", "top_p", "top_k", "thinking"} & set(body)


def test_default_request_is_unchanged_forced_tool_and_cached_system() -> None:
    client, bodies, _ = _client(_reply(searches=0))
    client.structured(_req(server_tools=[], tool_choice="tool", cache_system=True))
    body = bodies[0]
    assert body["tool_choice"] == {"type": "tool", "name": "record_web_findings"} and len(body["tools"]) == 1
    assert isinstance(body["system"], list) and body["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_search_count_cost_and_results_are_captured() -> None:
    client, _, settings = _client(_reply(searches=2, error_block=True))
    resp = client.structured(_req())
    assert resp.web_search_requests == 2
    assert resp.cost_usd_micros == cost_usd_micros(5000, 300, settings) + 2 * 10_000  # $10 / 1k searches
    assert resp.web_results == [{"url": "https://news.example/acme", "title": "Acme expands", "page_age": "2 days ago"}]
    assert resp.tool_input == {"summary": "s", "findings": [{"fact": "f", "url": "https://news.example/acme"}]}


def test_web_search_price_formula() -> None:
    s = Settings(_env_file=None)
    assert web_search_cost_usd_micros(0, s) == 0 and web_search_cost_usd_micros(3, s) == 30_000
    with pytest.raises(ValueError):
        web_search_cost_usd_micros(-1, s)
