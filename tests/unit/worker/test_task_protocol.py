"""q15 worker capability negotiation: old workers never see the task key; new workers fall back on an old server."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from sales0.config import Settings
from sales0.worker import protocol as p
from sales0.worker.client import WorkerClient
from sales0.worker.runner import Worker


class OldClaimResponse(p._Strict):  # noqa: SLF001 - the pre-q15 shape, strict like the shipped one
    run: p.ClaimedRun | None = None
    reason: str | None = None
    poll_s: float | None = None
    protocol: int = p.PROTOCOL_VERSION


def test_old_worker_rejects_a_task_key_so_the_server_must_omit_it() -> None:
    with pytest.raises(ValidationError):
        OldClaimResponse.model_validate({"reason": "x", "task": None})
    OldClaimResponse.model_validate(p.ClaimResponse(reason="x").model_dump(mode="json", exclude={"task"}))


def test_a_claim_without_capabilities_is_the_old_body() -> None:
    sent: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "data": {"reason": "nothing queued"}})

    client = WorkerClient("https://h.test", "t" * 40, http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _s: None)
    client.claim(p.ClaimRequest(worker=p.WorkerInfo(name="w")))
    client.claim(p.ClaimRequest(worker=p.WorkerInfo(name="w"), capabilities=["company_page"]))
    assert "capabilities" not in sent[0] and sent[1]["capabilities"] == ["company_page"]


def test_new_worker_drops_capabilities_once_an_old_server_answers_422() -> None:
    sent: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        sent.append(body)
        if "capabilities" in body:  # an old hosted app: extra=forbid -> 422
            return httpx.Response(422, json={"ok": False, "error": {"code": "invalid_request", "message": "extra fields"}})
        return httpx.Response(200, json={"ok": True, "data": {"reason": "nothing queued"}})

    client = WorkerClient("https://h.test", "t" * 40, http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _s: None)
    w = Worker(Settings(_env_file=None), client, sleep=lambda _s: None)
    assert w.poll_once() is False and w.poll_once() is False
    assert [("capabilities" in b) for b in sent] == [True, False, False]


def test_claimed_task_slug_is_bounded() -> None:
    with pytest.raises(ValidationError):
        p.ClaimedTask(task_id=1, kind="company_page", slug="x")
    with pytest.raises(ValidationError):
        p.TaskResultRequest(outcome="exploded")
