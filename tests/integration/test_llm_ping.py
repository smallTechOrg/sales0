"""Real Anthropic calls with the key from .env: the key and Haiku slug are alive, forced
tool use returns structured input, and bad key / unknown model map to clear errors."""

from __future__ import annotations

import subprocess
import sys

import pytest
from pydantic import SecretStr

from sales0.config import REPO_ROOT, get_settings
from sales0.llm import LLMAuthError, LLMModelNotFound, StructuredRequest, get_llm_client
from sales0.llm.anthropic_adapter import AnthropicClient
from sales0.llm.pricing import cost_usd_micros

pytestmark = pytest.mark.llm


def test_ping_reaches_the_configured_haiku_model_and_costs_tokens() -> None:
    settings = get_settings()
    resp = get_llm_client().ping()
    assert resp.input_tokens > 0 and resp.output_tokens > 0
    assert resp.cost_usd_micros == cost_usd_micros(resp.input_tokens, resp.output_tokens, settings) > 0
    assert resp.model.startswith(settings.llm_model)
    assert resp.text and "ok" in resp.text.lower()
    assert resp.latency_ms > 0


def test_structured_call_returns_the_forced_tool_input() -> None:
    req = StructuredRequest(
        feature="ping",
        system="You classify short phrases. Always answer by calling the tool.",
        messages=[{"role": "user", "content": "Classify the phrase 'red apple': give the colour word and the fruit word."}],
        tool_name="record_phrase",
        tool_description="Record the colour and fruit mentioned in the phrase.",
        input_schema={
            "type": "object",
            "properties": {"colour": {"type": "string"}, "fruit": {"type": "string"}},
            "required": ["colour", "fruit"],
        },
        max_tokens=100,
        prompt_version="scaffold-test-v1",
        log_context={"input_summary": "red apple"},
    )
    resp = get_llm_client().structured(req)
    assert resp.tool_input is not None
    assert resp.tool_input["colour"].lower() == "red"
    assert resp.tool_input["fruit"].lower() == "apple"
    assert resp.stop_reason == "tool_use"


def test_invalid_key_maps_to_llm_auth_error() -> None:
    bad = get_settings().model_copy(update={"anthropic_api_key": SecretStr("sk-ant-invalid-scaffold-test")})
    with pytest.raises(LLMAuthError, match="SALES0_ANTHROPIC_API_KEY"):
        AnthropicClient(bad).ping()


def test_unknown_model_maps_to_llm_model_not_found() -> None:
    bad = get_settings().model_copy(update={"llm_model": "claude-haiku-does-not-exist-0"})
    with pytest.raises(LLMModelNotFound, match="claude-haiku-does-not-exist-0"):
        AnthropicClient(bad).ping()


def test_check_llm_cli_prints_ok_with_the_model() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "sales0.cli", "check-llm"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=90
    )
    assert result.returncode == 0, result.stderr[-1000:]
    assert result.stdout.startswith(f"LLM OK model={get_settings().llm_model} input_tokens=")


def test_check_llm_cli_reports_a_typed_auth_error_for_a_bad_key() -> None:
    import os

    env = {**os.environ, "SALES0_ANTHROPIC_API_KEY": "sk-ant-invalid-cli-test"}
    result = subprocess.run(
        [sys.executable, "-m", "sales0.cli", "check-llm"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=90, env=env
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "LLM ERROR type=auth Anthropic rejected the key — check SALES0_ANTHROPIC_API_KEY in .env" in result.stderr
    assert "sk-ant-invalid-cli-test" not in result.stderr


def test_check_llm_cli_reports_model_not_found_for_an_unknown_slug() -> None:
    import os

    env = {**os.environ, "SALES0_LLM_MODEL": "claude-haiku-does-not-exist-0"}
    result = subprocess.run(
        [sys.executable, "-m", "sales0.cli", "check-llm"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=90, env=env
    )
    assert result.returncode == 1
    assert "LLM ERROR type=model-not-found Model 'claude-haiku-does-not-exist-0' not found" in result.stderr
