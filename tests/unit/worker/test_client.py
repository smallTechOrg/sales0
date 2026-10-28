"""WorkerClient: bearer header, typed payloads, retry/backoff on outages, no retry on 4xx."""

from __future__ import annotations

import httpx
import pytest

from sales0.worker import protocol as p
from sales0.worker.client import ServerUnavailable, WorkerApiError, WorkerAuthError, WorkerClient

CLAIM_EMPTY = {"ok": True, "data": {"run": None, "reason": "nothing queued", "poll_s": 15.0, "protocol": 1}}


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make(handler, ft: FakeTime | None = None, **kw) -> WorkerClient:
    ft = ft or FakeTime()
    return WorkerClient(
        "https://hosted.example/",
        "tok-123",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=ft.sleep,
        clock=ft.clock,
        **kw,
    )


def claim_req() -> p.ClaimRequest:
    return p.ClaimRequest(worker=p.WorkerInfo(name="laptop", version="0.1.0"))


def test_sends_bearer_token_and_json_and_parses_the_typed_reply() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=CLAIM_EMPTY)

    reply = make(handler).claim(claim_req())
    assert reply.run is None and reply.reason == "nothing queued"
    req = seen[0]
    assert req.url == "https://hosted.example/api/worker/claim"
    assert req.headers["authorization"] == "Bearer tok-123"
    assert req.headers["content-type"] == "application/json"
    assert b'"name":"laptop"' in req.content


def test_retries_outages_with_exponential_backoff_then_succeeds() -> None:
    ft = FakeTime()
    outcomes = iter([httpx.ConnectError("down"), httpx.Response(503), httpx.Response(502), httpx.Response(200, json=CLAIM_EMPTY)])

    def handler(request: httpx.Request) -> httpx.Response:
        item = next(outcomes)
        if isinstance(item, Exception):
            raise item
        return item

    client = make(handler, ft)
    reply = client._call("POST", "/api/worker/claim", claim_req(), p.ClaimResponse)  # noqa: SLF001
    assert reply.reason == "nothing queued"
    assert ft.sleeps == [1.0, 2.0, 4.0]


def test_gives_up_after_the_outage_budget() -> None:
    ft = FakeTime()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    client = make(handler, ft, outage_budget_s=10.0)
    with pytest.raises(ServerUnavailable):
        client.complete(7, p.CompleteRequest(outcome="finished"))
    assert sum(ft.sleeps) <= 10.0 and len(ft.sleeps) >= 2


def test_claim_polling_does_not_wait_out_a_long_outage() -> None:
    ft = FakeTime()
    client = make(lambda r: httpx.Response(503), ft)
    with pytest.raises(ServerUnavailable):
        client.claim(claim_req())
    assert sum(ft.sleeps) <= 5.0


def test_4xx_is_permanent_and_carries_the_envelope_message() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(409, json={"error": {"code": "out_of_order", "message": "Upload seq 3 first."}})

    with pytest.raises(WorkerApiError) as exc:
        make(handler).upload_posts(1, 2, p.UploadPostsRequest(seq=5))
    assert exc.value.code == "out_of_order" and "seq 3" in exc.value.message and len(calls) == 1


def test_401_is_an_auth_error_and_not_retried() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "bad token"}})

    with pytest.raises(WorkerAuthError):
        make(handler).claim(claim_req())
    assert len(calls) == 1


def test_payload_validation_rejects_unknown_fields_and_oversized_values() -> None:
    with pytest.raises(ValueError):
        p.PostPayload(source_ref="x", surprise=1)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        p.PostPayload(source_ref="x" * 301)
    with pytest.raises(ValueError):
        p.UploadPostsRequest(seq=0)
