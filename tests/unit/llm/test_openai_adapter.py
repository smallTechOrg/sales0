"""SMA-48: the OpenAI-compatible adapter - request shape, answer parsing, error mapping, retries (no network)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from sales0.config import get_settings
from sales0.llm import LLMAuthError, LLMBadResponse, LLMModelNotFound, LLMTransientError, StructuredRequest
from sales0.llm.openai_adapter import OpenAICompatibleClient, convert_messages
from sales0.llm.provider import ProviderConfig

KEY = "sk-test-secret-value-1234"


def cfg(**over: Any) -> ProviderConfig:
    base = dict(provider="openai", model="gpt-test", base_url="https://llm.example/v1", api_key=KEY,
                price_input_usd_per_mtok=1.0, price_output_usd_per_mtok=2.0, source="admin")  # fmt: skip
    return ProviderConfig(**{**base, **over})


def answer(args: dict[str, Any] | None = None, text: str | None = None, name: str = "record") -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if args is not None:
        message["tool_calls"] = [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]
    return {"id": "req-1", "model": "gpt-test-2026", "choices": [{"message": message, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}}  # fmt: skip


def client(handler, **over: Any) -> tuple[OpenAICompatibleClient, list[httpx.Request]]:  # noqa: ANN001
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request, len(seen))

    http = httpx.Client(transport=httpx.MockTransport(wrapped))
    return OpenAICompatibleClient(cfg(**over), get_settings(), http=http, sleep=lambda s: None), seen


def req(**over: Any) -> StructuredRequest:
    base = dict(feature="triage", system="Be brief.", messages=[{"role": "user", "content": "Hello"}], tool_name="record",
                tool_description="Record it", input_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
                max_tokens=50, prompt_version="t-v1")  # fmt: skip
    return StructuredRequest(**{**base, **over})


def test_forced_function_call_is_sent_and_parsed_with_usage_and_cost() -> None:
    c, seen = client(lambda r, n: httpx.Response(200, json=answer({"a": 7})))
    resp = c.structured(req())
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://llm.example/v1/chat/completions"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert body["model"] == "gpt-test" and body["max_tokens"] == 50
    assert body["messages"][0] == {"role": "system", "content": "Be brief."}
    assert body["tool_choice"] == {"type": "function", "function": {"name": "record"}}
    assert body["tools"][0]["function"]["parameters"]["properties"] == {"a": {"type": "integer"}}
    assert resp.tool_input == {"a": 7} and resp.model == "gpt-test-2026"
    assert (resp.input_tokens, resp.output_tokens, resp.cost_usd_micros) == (100, 20, 140)


def test_server_tools_and_effort_are_never_sent_and_the_research_model_is_used() -> None:
    s = get_settings()
    c, seen = client(lambda r, n: httpx.Response(200, json=answer({"a": 1})), research_model="big-model")
    c.structured(req(server_tools=[{"type": "web_search_20250305", "name": "web_search"}], tool_choice="auto",
                     effort="high", model=s.research_model))  # fmt: skip
    body = json.loads(seen[0].content)
    assert body["model"] == "big-model"
    assert len(body["tools"]) == 1 and "output_config" not in body
    assert body["tool_choice"]["type"] == "function"
    assert c.supports_web_search is False


def test_anthropic_style_repair_messages_become_tool_calls_and_tool_results() -> None:
    msgs = convert_messages("S", [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "record", "input": {"a": 1}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "bad a"}]},
    ])  # fmt: skip
    assert msgs[2]["tool_calls"][0]["function"] == {"name": "record", "arguments": '{"a": 1}'}
    assert msgs[3] == {"role": "tool", "tool_call_id": "t1", "content": "bad a"}


def test_json_written_as_text_is_accepted_when_the_endpoint_ignores_tools() -> None:
    c, _ = client(lambda r, n: httpx.Response(200, json=answer(None, text='```json\n{"a": 3}\n```')))
    assert c.structured(req()).tool_input == {"a": 3}


def test_a_server_that_rejects_a_named_tool_choice_is_retried_with_required() -> None:
    def handler(r: httpx.Request, n: int) -> httpx.Response:
        if json.loads(r.content)["tool_choice"] != "required":
            return httpx.Response(400, json={"error": {"message": "tool_choice must be auto or required"}})
        return httpx.Response(200, json=answer({"a": 2}))

    c, seen = client(handler)
    assert c.structured(req()).tool_input == {"a": 2} and len(seen) == 2


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (401, {"error": {"message": "Incorrect API key provided"}}, LLMAuthError),
        (402, {"error": {"message": "Insufficient credits"}}, LLMAuthError),
        (404, {"error": {"message": "The model `gpt-x` does not exist"}}, LLMModelNotFound),
        (400, {"error": {"message": "max_tokens too large"}}, LLMBadResponse),
    ],
)
def test_errors_are_mapped_with_the_providers_reason_and_never_the_key(status: int, body: dict, kind: type) -> None:
    c, seen = client(lambda r, n: httpx.Response(status, json=body))
    with pytest.raises(kind) as info:
        c.structured(req())
    assert body["error"]["message"] in info.value.message
    assert KEY not in info.value.message and len(seen) == 1


def test_429_and_5xx_are_retried_then_succeed() -> None:
    codes = iter([429, 503])
    c, seen = client(lambda r, n: httpx.Response(next(codes)) if n <= 2 else httpx.Response(200, json=answer({"a": 1})))
    assert c.structured(req()).tool_input == {"a": 1} and len(seen) == 3


def test_an_unreachable_endpoint_is_transient_after_the_retries() -> None:
    def handler(r: httpx.Request, n: int) -> httpx.Response:
        raise httpx.ConnectError("refused")

    c, seen = client(handler)
    with pytest.raises(LLMTransientError, match="could not be reached"):
        c.ping()
    assert len(seen) == get_settings().llm_max_retries + 1


def test_a_non_json_answer_says_to_check_the_url() -> None:
    c, _ = client(lambda r, n: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(LLMBadResponse, match="endpoint URL"):
        c.ping()


def test_missing_url_or_model_cannot_build_a_client() -> None:
    with pytest.raises(LLMBadResponse):
        OpenAICompatibleClient(cfg(base_url=""))
    with pytest.raises(LLMModelNotFound):
        OpenAICompatibleClient(cfg(model=""))
    with pytest.raises(LLMAuthError, match="enter it again"):
        OpenAICompatibleClient(cfg(api_key="", key_unreadable=True))


def test_no_key_sends_no_authorization_header_for_local_servers() -> None:
    c, seen = client(lambda r, n: httpx.Response(200, json=answer(None, text="OK")), api_key="", base_url="http://localhost:11434/v1/")
    assert c.ping().text == "OK"
    assert "authorization" not in seen[0].headers and str(seen[0].url) == "http://localhost:11434/v1/chat/completions"


def test_record_health_false_leaves_the_app_status_alone() -> None:
    from sales0.llm import health

    health.reset()
    c, _ = client(lambda r, n: httpx.Response(401, json={"error": {"message": "nope"}}))
    c._record = False
    with pytest.raises(LLMAuthError):
        c.ping()
    assert health.snapshot() == (None, None)
    c._record = True
    with pytest.raises(LLMAuthError):
        c.ping()
    assert health.snapshot()[0] is not None
    health.reset()
