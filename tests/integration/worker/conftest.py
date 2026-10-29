"""Hosted-mode fixtures: an app booted with SALES0_MODE=hosted + a worker token, a FakeLLM behind the worker API, and a
``WorkerClient`` that talks to it in-process (Starlette TestClient is an httpx.Client)."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from sales0.config import reset_settings
from sales0.jobs.runner import reset_runner
from sales0.web.app import create_app
from sales0.web.routes import worker_api
from sales0.worker.client import WorkerClient
from tests.support.fake_llm import FakeLLMClient, triage_answer

TOKEN = "t" * 40
BASE = "https://testserver"  # Secure cookies (the public URL is https) are only sent back over https


@pytest.fixture
def hosted_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "SALES0_MODE": "hosted",
        "SALES0_WORKER_TOKEN": TOKEN,
        "SALES0_SECRET_KEY": "s" * 32,
        "SALES0_PUBLIC_BASE_URL": "https://sales0.example.test",
        "SALES0_APP_PASSWORD": "correct horse battery",
        "SALES0_SAFETY_COOLDOWN_MIN": "60",
        "SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN": "15",
    }.items():
        monkeypatch.setenv(key, value)
    reset_settings()


@pytest.fixture
def llm() -> FakeLLMClient:
    return FakeLLMClient(responder=lambda req: triage_answer())


@pytest.fixture
def hosted(db_url: str, hosted_env: None, llm: FakeLLMClient) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[worker_api.get_llm] = lambda: llm
    with TestClient(app, base_url=BASE) as client:
        yield client
    reset_runner()


@pytest.fixture
def worker_client(hosted: TestClient) -> WorkerClient:
    return WorkerClient(BASE, TOKEN, http=hosted, sleep=lambda _s: None)


class FlakyHttp:
    """Wraps the in-process client; the first ``failures`` requests die like a hosted-side outage."""

    def __init__(self, inner: httpx.Client, failures: int, kind: str = "connect") -> None:
        self.inner, self.failures, self.kind, self.calls = inner, failures, kind, 0

    def request(self, method: str, url: str, **kw) -> httpx.Response:  # noqa: ANN003
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            if self.kind == "503":
                return httpx.Response(503, json={"ok": False, "error": {"code": "unavailable", "message": "cold start"}})
            raise httpx.ConnectError("hosted app is down")
        return self.inner.request(method, url, **kw)

    def close(self) -> None:
        pass
