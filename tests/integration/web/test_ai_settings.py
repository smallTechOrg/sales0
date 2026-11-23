"""SMA-48: an admin chooses the AI provider (OpenAI-compatible endpoint + model + key), tests it, and the whole app
uses it; the key is stored encrypted and never shown; the AI status names the provider's own problem."""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from sales0.db.engine import session_scope
from sales0.db.models import AiProviderConfig, TeamMember
from sales0.llm import get_llm_client, health, provider, reset_llm_client, supports_web_search
from sales0.llm.openai_adapter import OpenAICompatibleClient
from sales0.web.app import create_app

KEY = "sk-live-do-not-leak-9876"
FORM = {"provider": "openai", "base_url": "https://llm.example/v1/chat/completions/", "model": "gpt-test",
        "research_model": "", "api_key": KEY, "price_in": "0.5", "price_out": "1.5"}  # fmt: skip


@pytest.fixture
def web(db_url: str) -> Iterator[TestClient]:
    with session_scope() as db:
        db.add(TeamMember(name="Ada Admin", email="ada@example.com", role="admin"))
    reset_llm_client()
    health.reset()
    with TestClient(create_app()) as c:
        yield c
    reset_llm_client()
    health.reset()


def _fake_http(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:  # noqa: ANN001
    """Route every OpenAI-compatible client the app builds through ``handler`` (no network)."""
    seen: list[httpx.Request] = []
    real_init = OpenAICompatibleClient.__init__

    def init(self, config, settings=None, **kw):  # noqa: ANN001, ANN202
        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        kw["http"] = httpx.Client(transport=httpx.MockTransport(wrapped))
        real_init(self, config, settings, **kw)

    monkeypatch.setattr(OpenAICompatibleClient, "__init__", init)
    return seen


def _ok(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    message: dict = {"role": "assistant", "content": "OK"}
    if body.get("tools"):
        message = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": body["tools"][0]["function"]["name"], "arguments": '{"ok": true, "word": "ready"}'}}]}  # fmt: skip
    return httpx.Response(200, json={"id": "r1", "model": body["model"], "choices": [{"message": message}],
                                     "usage": {"prompt_tokens": 5, "completion_tokens": 2}})  # fmt: skip


def test_page_shows_anthropic_from_the_server_settings_by_default(web: TestClient) -> None:
    page = web.get("/settings/ai")
    assert page.status_code == 200 and 'id="ai-settings-form"' in page.text
    assert "Anthropic, from the server settings" in page.text
    assert 'href="/settings/ai"' in page.text  # admin nav item


def test_save_stores_the_key_encrypted_and_the_app_switches_provider(web: TestClient) -> None:
    resp = web.post("/settings/ai", data=FORM, follow_redirects=False)
    assert resp.status_code == 303
    with session_scope() as db:
        row = db.get(AiProviderConfig, 1)
        assert row is not None and row.provider == "openai" and row.base_url == "https://llm.example/v1"
        assert row.api_key_encrypted and KEY not in row.api_key_encrypted
    page = web.get("/settings/ai").text
    assert KEY not in page and "...9876" in page and "OpenAI-compatible: <code>https://llm.example/v1</code>" in page
    client = get_llm_client()
    assert isinstance(client, OpenAICompatibleClient) and client.model == "gpt-test"
    assert supports_web_search() is False
    # An empty key field keeps the stored key.
    web.post("/settings/ai", data={**FORM, "api_key": "", "model": "gpt-other"}, follow_redirects=False)
    assert provider.current(refresh=True).api_key == KEY and get_llm_client().model == "gpt-other"
    # Back to Anthropic.
    web.post("/settings/ai", data={"provider": "anthropic"}, follow_redirects=False)
    assert provider.current(refresh=True).provider == "anthropic" and supports_web_search() is True


@pytest.mark.parametrize(
    ("over", "message"),
    [({"base_url": "ftp://x"}, "must start with https://"), ({"base_url": "http://169.254.169.254/v1"}, "cannot be used"),
     ({"base_url": "https://user:pw@llm.example/v1"}, "not in the URL"), ({"model": ""}, "Enter the model name"),
     ({"price_in": "abc"}, "Prices are numbers")],
)
def test_bad_values_are_refused_and_nothing_is_saved(web: TestClient, over: dict, message: str) -> None:
    resp = web.post("/settings/ai", data={**FORM, **over})
    assert resp.status_code == 422 and message in resp.text and KEY not in resp.text
    with session_scope() as db:
        assert db.get(AiProviderConfig, 1) is None


def test_test_button_checks_reply_and_function_calling_without_saving(web: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_http(monkeypatch, _ok)
    page = web.post("/settings/ai/test", data=FORM)
    assert page.status_code == 200 and 'id="ai-test-result"' in page.text
    assert "Working: OpenAI-compatible (llm.example) answered with model gpt-test" in page.text
    assert len(seen) == 2 and seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in page.text
    with session_scope() as db:
        assert db.get(AiProviderConfig, 1) is None  # nothing saved
    assert health.snapshot() == (None, None)  # the app's AI status is untouched


def test_test_button_reports_a_model_without_function_calling(web: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_http(monkeypatch, lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}))
    page = web.post("/settings/ai/test", data=FORM).text
    assert "Not working: The model answered but did not fill in the test form" in page


def test_test_button_shows_the_providers_reason(web: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_http(monkeypatch, lambda r: httpx.Response(401, json={"error": {"message": "Incorrect API key provided"}}))
    page = web.post("/settings/ai/test", data=FORM).text
    assert "Not working: The AI provider rejected the key" in page and "Incorrect API key provided" in page


def test_a_failing_provider_shows_the_ai_banner_with_the_fix(web: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_http(monkeypatch, lambda r: httpx.Response(401, json={"error": {"message": "bad key"}}))
    web.post("/settings/ai", data=FORM, follow_redirects=False)
    assert web.post("/health/ai-check", follow_redirects=False).status_code == 303
    health_page = web.get("/health").text
    assert "AI (OpenAI-compatible (llm.example))" in health_page
    assert "the AI provider rejected the API key" in health_page and "AI settings page" in health_page


def test_members_cannot_open_the_page(web: TestClient) -> None:
    with session_scope() as db:
        bob = TeamMember(name="Bob Member", email="bob@example.com", role="member")
        db.add(bob)
        db.flush()
        bob_id = bob.id
    assert web.post("/team/acting", data={"member_id": bob_id, "next": "/health"}, follow_redirects=False).status_code == 303
    assert web.post("/settings/ai", data=FORM, headers={"Accept": "application/json"}).status_code == 403
    assert web.get("/settings/ai", headers={"Accept": "application/json"}).status_code == 403
    assert 'href="/settings/ai"' not in web.get("/health").text
