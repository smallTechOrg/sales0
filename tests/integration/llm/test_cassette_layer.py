"""The record/replay layer (tests/llm_cassette.py) driven by a fake "real" SDK: no network, no key."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from pydantic import SecretStr

from sales0.config import Settings
from sales0.llm import LLMAuthError, LLMBadResponse, StructuredRequest
from sales0.llm.anthropic_adapter import AnthropicClient
from tests import llm_cassette as lc


def _message(tool_input: dict[str, Any], *, cache_write: int = 0, cache_read: int = 0, rid: str = "msg_1") -> Any:
    return anthropic.types.Message.model_validate(
        {
            "id": rid, "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "stop_reason": "tool_use",
            "stop_sequence": None,
            "content": [{"type": "tool_use", "id": "tu_1", "name": "record_word", "input": tool_input}],
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": cache_write,
                      "cache_read_input_tokens": cache_read},
        }
    )


class FakeRealSDK:
    """Stands in for anthropic.Anthropic when recording."""

    def __init__(self, script: list[Any]) -> None:
        self.script, self.calls = script, []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **params: Any) -> Any:
        self.calls.append(params)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _req(word: str = "hello", system: str = "sys 2026-10-29T10:00:00Z") -> StructuredRequest:
    return StructuredRequest(
        feature="ping", system=system, messages=[{"role": "user", "content": f"Word: {word}"}],
        tool_name="record_word", tool_description="d",
        input_schema={"type": "object", "properties": {"word": {"type": "string"}}}, max_tokens=50, prompt_version="t-v1",
    )


def _settings(key: str = "sk-test") -> Settings:
    return Settings(_env_file=None, anthropic_api_key=SecretStr(key), llm_max_retries=0)


def _sdk(path: Path, *, record: bool, script: list[Any] | None = None, key: str = "sk-real") -> lc.CassetteSDK:
    cassette = lc.Cassette(path, record=record, fresh=record)
    fake = FakeRealSDK(script or [])
    return lc.CassetteSDK(cassette, api_key=key, real_factory=lambda **kw: fake, kwargs={})


def test_record_then_replay_round_trips_through_the_real_adapter(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    rec = AnthropicClient(_settings(), sdk=_sdk(path, record=True, script=[_message({"word": "hello"})]))
    first = rec.structured(_req())
    assert first.tool_input == {"word": "hello"} and path.exists()

    replay = AnthropicClient(_settings(), sdk=_sdk(path, record=False))  # nothing scripted: must not call out
    second = replay.structured(_req())
    assert second.tool_input == {"word": "hello"}
    assert (second.input_tokens, second.output_tokens, second.cost_usd_micros) == (
        first.input_tokens, first.output_tokens, first.cost_usd_micros)
    assert second.request_id == "msg_1" and second.stop_reason == "tool_use" and second.latency_ms > 0


def test_replay_ignores_dates_and_uuids_but_not_real_prompt_changes(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    AnthropicClient(_settings(), sdk=_sdk(path, record=True, script=[_message({"word": "a"})])).structured(_req())
    replay = AnthropicClient(_settings(), sdk=_sdk(path, record=False))
    assert replay.structured(_req(system="sys 2031-01-02T03:04:05Z")).tool_input == {"word": "a"}
    with pytest.raises(LLMBadResponse):  # the adapter maps SDK errors; the miss is also kept for the fixture to fail on
        replay.structured(_req(word="different"))
    assert len(lc.MISSES) == 1 and "record_llm_cassettes.sh" in lc.MISSES[0]
    lc.MISSES.clear()


def test_repeated_identical_requests_replay_in_order_and_the_last_repeats(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    script = [_message({"word": "x"}, cache_write=700, rid="m1"), _message({"word": "x"}, cache_read=700, rid="m2")]
    rec = AnthropicClient(_settings(), sdk=_sdk(path, record=True, script=script))
    rec.structured(_req())
    rec.structured(_req())
    replay = AnthropicClient(_settings(), sdk=_sdk(path, record=False))
    got = [replay.structured(_req()) for _ in range(3)]
    assert [(r.cache_creation_input_tokens, r.cache_read_input_tokens) for r in got] == [(700, 0), (0, 700), (0, 700)]


def test_errors_are_recorded_and_replayed_as_the_same_mapped_error(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    response = httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    err = anthropic.AuthenticationError("invalid x-api-key", response=response, body={"type": "error"})
    rec = AnthropicClient(_settings("sk-ant-invalid-x"), sdk=_sdk(path, record=True, script=[err], key="sk-ant-invalid-x"))
    with pytest.raises(LLMAuthError):
        rec.structured(_req())
    replay = AnthropicClient(_settings("sk-ant-invalid-x"), sdk=_sdk(path, record=False, key="sk-ant-invalid-x"))
    with pytest.raises(LLMAuthError, match="SALES0_ANTHROPIC_API_KEY"):
        replay.structured(_req())


def test_a_bad_key_does_not_collide_with_the_good_key(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    AnthropicClient(_settings(), sdk=_sdk(path, record=True, script=[_message({"word": "ok"})])).structured(_req())
    bad = AnthropicClient(_settings("sk-ant-invalid"), sdk=_sdk(path, record=False, key="sk-ant-invalid"))
    with pytest.raises(LLMBadResponse):
        bad.structured(_req())
    assert len(lc.MISSES) == 1
    lc.MISSES.clear()


def test_digit_only_differences_fall_back_to_the_loose_key(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    AnthropicClient(_settings(), sdk=_sdk(path, record=True, script=[_message({"word": "n"})])).structured(_req(word="run 41"))
    replay = AnthropicClient(_settings(), sdk=_sdk(path, record=False))
    assert replay.structured(_req(word="run 4242")).tool_input == {"word": "n"}


def test_module_cassette_path_and_refresh_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    p = lc.cassette_path_for("tests/integration/suggest/test_real_haiku.py::test_x[a-b]")
    assert p == lc.CASSETTE_ROOT / "integration" / "suggest" / "test_real_haiku.json"
    monkeypatch.delenv(lc.REFRESH_ENV, raising=False)
    assert lc.refresh_mode() is False
    monkeypatch.setenv(lc.REFRESH_ENV, "1")
    assert lc.refresh_mode() is True


def test_install_patches_and_restores_anthropic_anthropic(tmp_path: Path) -> None:
    real = anthropic.Anthropic
    undo = lc.install(tmp_path / "c.json", record=False)
    try:
        sdk = anthropic.Anthropic(api_key="sk-x", max_retries=0)
        assert isinstance(sdk, lc.CassetteSDK)
    finally:
        undo()
    assert anthropic.Anthropic is real


def test_a_subprocess_gets_the_cassette_through_sitecustomize(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    env = {
        **os.environ,
        lc.FILE_ENV: str(tmp_path / "c.json"),
        lc.REFRESH_ENV: "0",
        "PYTHONPATH": os.pathsep.join([str(lc.BOOT_DIR), str(lc.REPO_ROOT)]),
    }
    code = "import anthropic; print(type(anthropic.Anthropic(api_key='sk-x')).__name__)"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60, cwd=tmp_path)
    assert out.stdout.strip() == "CassetteSDK", out.stderr[-500:]
