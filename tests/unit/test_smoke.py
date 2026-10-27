"""Pure scaffold checks: settings, pricing, clock, filters, envelope, LLM adapter logic.

No network: the adapter is exercised with a fake SDK object.
"""

from __future__ import annotations

import json
import os
import random
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import SecretStr, ValidationError

from sales0.clock import local_day_bounds_utc, local_month_bounds_utc, local_today
from sales0.config import REPO_ROOT, Settings, StartupError, check_startup
from sales0.errors import DomainError
from sales0.llm import LLMAuthError, LLMBadResponse, LLMModelNotFound, LLMTransientError, StructuredRequest
from sales0.llm.errors import LLMWorkspaceRequired
from sales0.llm.anthropic_adapter import AnthropicClient
from sales0.llm.pricing import cost_usd_micros
from sales0.web.envelope import err, ok
from sales0.web.templating import ago, usd, verdict_label

FAKE_KEY = SecretStr("not-a-real-key")


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"anthropic_api_key": FAKE_KEY, **overrides}
    return Settings(_env_file=None, **values)


# --------------------------------------------------------------------------- settings
def test_settings_defaults_match_the_configuration_table(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("SALES0_"):
            monkeypatch.delenv(key)
    s = Settings(_env_file=None)
    assert s.llm_model == "claude-haiku-5-5"
    assert (s.host, s.port) == ("127.0.0.1", 8765)
    assert s.llm_max_output_tokens == 900 and s.llm_max_retries == 3 and s.llm_timeout_s == 30
    assert (s.llm_price_input_usd_per_mtok, s.llm_price_output_usd_per_mtok) == (0.10, 0.50)
    assert s.database_url == "sqlite:///data/sales0.db"
    assert s.resolved_database_url == f"sqlite:///{(REPO_ROOT / 'data/sales0.db').as_posix()}"
    assert s.data_path == REPO_ROOT / "data"
    assert s.timezone == "Asia/Kolkata" and s.linkedin_mode == "live" and s.log_format == "json"
    assert s.linkedin_browser_channel == "auto" and s.scrape_date_filter == "past-week"
    assert (s.scrape_max_keywords_per_run, s.scrape_max_posts_per_keyword) == (10, 15)
    assert (s.scrape_max_posts_per_day, s.scrape_max_runs_per_day) == (150, 3)
    assert (s.fewshot_max_examples, s.fewshot_text_chars, s.triage_max_input_chars) == (6, 600, 4000)
    assert s.api_key_present is False


def test_enum_env_values_strip_inline_comments_and_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_LINKEDIN_MODE", "  fixture   # offline demo")
    monkeypatch.setenv("SALES0_LOG_FORMAT", "Console # dev")
    s = make_settings()
    assert s.linkedin_mode == "fixture"
    assert s.log_format == "console"


def test_invalid_linkedin_mode_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_LINKEDIN_MODE", "staging")
    with pytest.raises(ValidationError):
        make_settings()


def test_inverted_delay_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(scrape_delay_min_s=10, scrape_delay_max_s=2)


def test_api_key_is_never_rendered_by_repr_or_dump() -> None:
    s = make_settings()
    assert "not-a-real-key" not in repr(s)
    assert "not-a-real-key" not in str(s.model_dump())
    assert s.api_key_present is True


def test_startup_refuses_missing_key_and_public_host() -> None:
    with pytest.raises(StartupError, match="SALES0_ANTHROPIC_API_KEY"):
        check_startup(Settings(_env_file=None, anthropic_api_key=None))
    with pytest.raises(StartupError, match="SALES0_APP_PASSWORD"):
        check_startup(make_settings(host="0.0.0.0"))
    check_startup(make_settings(host="localhost"))


# --------------------------------------------------------------------------- pricing / clock / filters
def test_cost_formula_is_tokens_times_price_per_mtok_in_micros() -> None:
    s = make_settings()
    assert cost_usd_micros(1500, 200, s) == 250  # 1500 x $0.10 + 200 x $0.50 per MTok
    assert cost_usd_micros(0, 0, s) == 0
    with pytest.raises(ValueError):
        cost_usd_micros(-1, 0, s)


def test_local_today_uses_the_configured_timezone() -> None:
    late_utc = datetime(2026, 10, 27, 20, 0, tzinfo=UTC)  # 01:30 next day in Kolkata
    assert local_today("Asia/Kolkata", late_utc) == date(2026, 10, 28)
    start, end = local_day_bounds_utc("Asia/Kolkata", late_utc)
    assert end - start == timedelta(days=1) and start <= late_utc < end
    m_start, m_end = local_month_bounds_utc("Asia/Kolkata", datetime(2026, 12, 15, tzinfo=UTC))
    assert m_start.month == 11 and m_end.year == 2026 and m_end.month == 12  # IST midnight is previous UTC day


def test_usd_filter_shows_four_decimals_below_one_cent() -> None:
    assert usd(1200) == "$0.0012"
    assert usd(0) == "$0.00"
    assert usd(1_234_567) == "$1.23"
    assert usd(None) == "—"


def test_ago_and_verdict_label_filters() -> None:
    now = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    assert ago(now - timedelta(seconds=5), now) == "just now"
    assert ago(now - timedelta(hours=3), now) == "3h ago"
    assert ago(None, now) == "never"
    assert verdict_label("genuine") == "Genuine" and verdict_label(None) == "Not triaged"


def test_envelope_shapes_and_domain_error() -> None:
    assert json.loads(ok({"a": 1}).body) == {"ok": True, "data": {"a": 1}}
    bad = err("not_found", "Gone.", 404)
    assert bad.status_code == 404
    assert json.loads(bad.body) == {"ok": False, "error": {"code": "not_found", "message": "Gone."}}
    exc = DomainError("job_busy", "LinkedIn is busy.", 409)
    assert (exc.code, exc.message, exc.http_status) == ("job_busy", "LinkedIn is busy.", 409)


# --------------------------------------------------------------------------- adapter (fake SDK)
_REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    return cls("boom", response=httpx2.Response(status, request=_REQ), body=None)


class FakeMessages:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _message(tool_input: dict[str, Any] | None = None, text: str | None = None) -> Any:
    content = []
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    if tool_input is not None:
        content.append(SimpleNamespace(type="tool_use", name="record_echo", input=tool_input))
    return SimpleNamespace(
        content=content,
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        model="claude-haiku-5-5",
        stop_reason="tool_use" if tool_input else "end_turn",
        id="msg_1",
    )


def _client(outcomes: list[Any], **settings: Any) -> tuple[AnthropicClient, FakeMessages, list[float]]:
    messages = FakeMessages(outcomes)
    sleeps: list[float] = []
    client = AnthropicClient(
        make_settings(**settings), sdk=SimpleNamespace(messages=messages), sleep=sleeps.append, rng=random.Random(1)
    )
    return client, messages, sleeps


def _request() -> StructuredRequest:
    return StructuredRequest(
        feature="triage",
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        tool_name="record_echo",
        tool_description="Echo",
        input_schema={"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]},
        max_tokens=50,
        prompt_version="t-v1",
    )


def test_structured_forces_the_tool_and_returns_usage_and_cost() -> None:
    client, messages, _ = _client([_message({"word": "hello"})])
    resp = client.structured(_request())
    sent = messages.calls[0]
    assert sent["tool_choice"] == {"type": "tool", "name": "record_echo"}
    assert sent["tools"][0]["name"] == "record_echo" and sent["model"] == "claude-haiku-5-5"
    assert resp.tool_input == {"word": "hello"}
    assert (resp.input_tokens, resp.output_tokens, resp.cost_usd_micros) == (100, 20, 10 + 10)


def test_structured_returns_none_tool_input_when_model_skips_the_tool() -> None:
    client, _, _ = _client([_message(text="no tool")])
    resp = client.structured(_request())
    assert resp.tool_input is None and resp.text == "no tool"


def test_transient_errors_are_retried_with_backoff_then_succeed() -> None:
    outcomes = [_status_error(anthropic.RateLimitError, 429), anthropic.APITimeoutError(request=_REQ), _message({"word": "x"})]
    client, messages, sleeps = _client(outcomes)
    assert client.structured(_request()).tool_input == {"word": "x"}
    assert len(messages.calls) == 3 and len(sleeps) == 2 and sleeps[1] > sleeps[0] >= 1.0


def test_retries_are_capped_and_raise_transient_error() -> None:
    outcomes = [_status_error(anthropic.InternalServerError, 500)] * 3
    client, messages, _ = _client(outcomes, llm_max_retries=2)
    with pytest.raises(LLMTransientError):
        client.structured(_request())
    assert len(messages.calls) == 3


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_status_error(anthropic.AuthenticationError, 401), LLMAuthError),
        (_status_error(anthropic.PermissionDeniedError, 403), LLMAuthError),
        (_status_error(anthropic.NotFoundError, 404), LLMModelNotFound),
        (_status_error(anthropic.BadRequestError, 400), LLMBadResponse),
    ],
)
def test_fatal_errors_are_mapped_without_retry(exc: Exception, expected: type[Exception]) -> None:
    client, messages, sleeps = _client([exc])
    with pytest.raises(expected):
        client.structured(_request())
    assert len(messages.calls) == 1 and sleeps == []


def test_auth_error_message_names_the_env_var_not_the_key() -> None:
    client, _, _ = _client([_status_error(anthropic.AuthenticationError, 401)])
    with pytest.raises(LLMAuthError) as info:
        client.ping()
    assert "SALES0_ANTHROPIC_API_KEY" in str(info.value) and "not-a-real-key" not in str(info.value)


def test_client_refuses_to_build_without_a_key() -> None:
    with pytest.raises(LLMAuthError):
        AnthropicClient(Settings(_env_file=None, anthropic_api_key=None))


def test_unscoped_key_400_maps_to_auth_error_naming_the_workspace_setting() -> None:
    body = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "This API key is not scoped to a workspace, so this request must include the "
            "anthropic-workspace-id header with the ID of the workspace to use.",
        },
    }
    exc = anthropic.BadRequestError("boom", response=httpx2.Response(400, request=_REQ), body=body)
    client, messages, _ = _client([exc])
    with pytest.raises(LLMWorkspaceRequired) as info:
        client.ping()
    assert isinstance(info.value, LLMAuthError)  # existing fatal-auth handlers still catch it
    assert info.value.error_type == "workspace_required"
    assert str(info.value) == (
        "This Anthropic key is not workspace-scoped — set SALES0_ANTHROPIC_WORKSPACE_ID in .env"
    )
    assert len(messages.calls) == 1


# --------------------------------------------------------------------------- check-llm CLI
class _FakePingClient:
    outcome: Any = None

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def ping(self) -> Any:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.parametrize(
    ("exc", "label"),
    [
        (LLMWorkspaceRequired("This Anthropic key is not workspace-scoped — set SALES0_ANTHROPIC_WORKSPACE_ID in .env"),
         "workspace-required"),
        (LLMAuthError("Anthropic rejected the key — check SALES0_ANTHROPIC_API_KEY in .env"), "auth"),
        (LLMModelNotFound("Model 'x' not found — check SALES0_LLM_MODEL"), "model-not-found"),
        (LLMTransientError("LLM unavailable — retry later (APITimeoutError)"), "transient"),
        (LLMBadResponse("Anthropic rejected the request (HTTP 400, BadRequestError)"), "bad-response"),
    ],
)
def test_check_llm_prints_typed_error_line_and_exits_1(
    exc: Exception, label: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sales0.llm.anthropic_adapter as adapter
    from sales0 import cli

    fake = type("Fake", (_FakePingClient,), {"outcome": exc})
    monkeypatch.setattr(adapter, "AnthropicClient", fake)
    assert cli.main(["check-llm"]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    error_line = [x for x in out.err.splitlines() if x.startswith("LLM ERROR")]
    assert error_line == [f"LLM ERROR type={label} {exc}"]


def test_check_llm_success_line_is_unchanged(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import sales0.llm.anthropic_adapter as adapter
    from sales0 import cli
    from sales0.config import get_settings

    fake = type("Fake", (_FakePingClient,), {"outcome": SimpleNamespace(input_tokens=9, output_tokens=2)})
    monkeypatch.setattr(adapter, "AnthropicClient", fake)
    assert cli.main(["check-llm"]) == 0
    assert capsys.readouterr().out == f"LLM OK model={get_settings().llm_model} input_tokens=9 output_tokens=2\n"


def test_workspace_id_is_sent_as_a_default_header_when_configured() -> None:
    client = AnthropicClient(make_settings(anthropic_workspace_id="wrkspc_test123"))
    assert client._sdk.default_headers.get("anthropic-workspace-id") == "wrkspc_test123"
    plain = AnthropicClient(make_settings())
    assert "anthropic-workspace-id" not in plain._sdk.default_headers


def test_log_context_keys_colliding_with_fixed_log_fields_are_prefixed_not_raised(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sales0.config import Settings as _S
    from sales0.logging import configure_logging

    configure_logging(_S(_env_file=None, log_format="json", log_level="INFO"))
    client, _, _ = _client([_message({"word": "hello"})])
    req = _request()
    req.log_context = {
        "status": "ctx-status",
        "model": "ctx-model",
        "feature": "ctx-feature",
        "attempts": 99,
        "run_id": 7,
        "fewshot_count": 2,
        "input_summary": "post urn:li:activity:1",
    }
    resp = client.structured(req)  # must not raise TypeError after the (paid) call
    assert resp.tool_input == {"word": "hello"}
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    line = next(x for x in lines if x.get("event") == "llm_call")
    assert line["status"] == "ok" and line["model"] == "claude-haiku-5-5" and line["feature"] == "triage"
    assert line["attempts"] == 1 and line["run_id"] == 7 and line["fewshot_count"] == 2
    assert line["ctx_status"] == "ctx-status" and line["ctx_model"] == "ctx-model"
    assert line["ctx_feature"] == "ctx-feature" and line["ctx_attempts"] == 99
