"""HTTP client for ``/api/worker/*``: bearer auth, retry with exponential backoff, typed payloads.

The hosted app scales to zero and can be redeployed mid-run, so a connection error, timeout or 429/5xx is
retried (1, 2, 4 ... 60 s) until ``outage_budget_s`` is used up; only then does ``ServerUnavailable`` escape and
the caller gives up (the hosted side marks a silent run lost after ``SALES0_WORKER_STALE_S``). Any other 4xx is a
permanent ``WorkerApiError``. Uploads are idempotent (per-keyword ``seq``), which is what makes retrying safe.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from sales0.logging import get_logger
from sales0.worker import protocol as p

log = get_logger("sales0.worker.client")

T = TypeVar("T", bound=BaseModel)
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class WorkerApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class WorkerAuthError(WorkerApiError):
    """401/403: the token is wrong. Retrying cannot help."""


class ServerUnavailable(RuntimeError):
    """The hosted app stayed unreachable for the whole outage budget."""


class WorkerClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout_s: float = 120.0,
        outage_budget_s: float = 1800.0,
        backoff_max_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=timeout_s)
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self._sleep = sleep
        self._outage_budget_s = outage_budget_s
        self._backoff_max_s = backoff_max_s
        self._clock = clock

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ transport
    def _call(
        self, method: str, path: str, body: BaseModel | None, model: type[T], *, budget_s: float | None = None,
        exclude: set[str] | None = None,
    ) -> T:
        budget = self._outage_budget_s if budget_s is None else budget_s
        started = self._clock()
        delay = 1.0
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._send(method, path, body, exclude)
            except httpx.TransportError as exc:
                failure = f"{type(exc).__name__}"
            else:
                if response.status_code < 400:
                    body = response.json()
                    # the app's envelope is {"ok": true, "data": {...}}; accept a bare object too
                    return model.model_validate(body["data"] if isinstance(body, dict) and "data" in body else body)
                if response.status_code not in RETRY_STATUSES:
                    raise self._error(response)
                failure = f"HTTP {response.status_code}"
            elapsed = self._clock() - started
            if elapsed + delay > budget:
                raise ServerUnavailable(f"{self._base}{path} unreachable ({failure}) after {attempt} attempt(s)")
            log.warning("worker_api_retry", path=path, failure=failure, attempt=attempt, wait_s=delay)
            self._sleep(delay)
            delay = min(delay * 2, self._backoff_max_s)

    def _send(self, method: str, path: str, body: BaseModel | None, exclude: set[str] | None = None) -> httpx.Response:
        headers = dict(self._headers)
        content: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = body.model_dump_json(exclude=exclude).encode("utf-8")
        return self._http.request(method, self._base + path, headers=headers, content=content)

    @staticmethod
    def _error(response: httpx.Response) -> WorkerApiError:
        code, message = "error", response.text[:200]
        try:
            data: Any = response.json()
            err = data.get("error") if isinstance(data, dict) else None
            if isinstance(err, dict):
                code, message = str(err.get("code", code)), str(err.get("message", message))
            elif isinstance(data, dict) and "detail" in data:
                message = str(data["detail"])[:300]
        except ValueError:
            pass
        cls = WorkerAuthError if response.status_code in (401, 403) else WorkerApiError
        return cls(response.status_code, code, message)

    # ------------------------------------------------------------------ endpoints
    def claim(self, request: p.ClaimRequest) -> p.ClaimResponse:
        # Idle polling: one quick retry only; the poll loop owns the long backoff. No capabilities = the exact
        # pre-q15 body (an older hosted app rejects unknown fields).
        exclude = None if request.capabilities else {"capabilities"}
        return self._call("POST", "/api/worker/claim", request, p.ClaimResponse, budget_s=5.0, exclude=exclude)

    def task_result(self, task_id: int, request: p.TaskResultRequest) -> p.TaskResultResponse:
        return self._call("POST", f"/api/worker/tasks/{task_id}/result", request, p.TaskResultResponse)

    def heartbeat(self, run_id: int, request: p.HeartbeatRequest) -> p.HeartbeatResponse:
        return self._call("POST", f"/api/worker/runs/{run_id}/heartbeat", request, p.HeartbeatResponse, budget_s=20.0)

    def upload_posts(self, run_id: int, row_id: int, request: p.UploadPostsRequest) -> p.KeywordAck:
        return self._call("POST", f"/api/worker/runs/{run_id}/keywords/{row_id}/posts", request, p.KeywordAck)

    def upload_links(self, run_id: int, row_id: int, request: p.UploadLinksRequest) -> p.UploadLinksResponse:
        return self._call("POST", f"/api/worker/runs/{run_id}/keywords/{row_id}/links", request, p.UploadLinksResponse)

    def complete(self, run_id: int, request: p.CompleteRequest) -> p.CompleteResponse:
        return self._call("POST", f"/api/worker/runs/{run_id}/complete", request, p.CompleteResponse)

    def import_batch(self, request: p.ImportBatchRequest) -> p.ImportBatchResponse:
        return self._call("POST", "/api/worker/import/batch", request, p.ImportBatchResponse)

    def import_deck_chunk(self, request: p.ImportDeckChunkRequest) -> p.ImportDeckChunkResponse:
        return self._call("POST", "/api/worker/import/deck-chunk", request, p.ImportDeckChunkResponse)

    def import_finalize(self, request: p.ImportFinalizeRequest) -> p.ImportFinalizeResponse:
        return self._call("POST", "/api/worker/import/finalize", request, p.ImportFinalizeResponse)
