"""``/api/worker/*`` - the laptop worker's API (bearer token, JSON). See ``sales0.services.worker_service``.

Not behind the login: the session middleware lists ``/api/worker/`` as open and THIS module authenticates every
request with ``SALES0_WORKER_TOKEN`` (constant-time compare). With no token configured the routes answer 404, so a
plain local install exposes nothing.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.engine import get_db
from sales0.errors import DomainError
from sales0.llm import LLMClient, get_llm_client
from sales0.logging import get_logger
from sales0.services import data_import, worker_service
from sales0.web.envelope import ok
from sales0.worker import protocol as p

router = APIRouter(prefix="/api/worker")
log = get_logger("sales0.web.worker_api")


def get_llm() -> LLMClient:
    """LLM seam (override with ``app.dependency_overrides`` in tests)."""
    return get_llm_client()


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def require_worker(request: Request) -> None:
    expected = get_settings().worker_token_value
    if not expected:
        raise DomainError("not_found", "That item no longer exists - refresh the page.", 404)
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    good = scheme.lower() == "bearer" and hmac.compare_digest(_digest(supplied.strip()), _digest(expected))
    if not good:
        log.warning("worker_auth_rejected", client=request.client.host if request.client else "unknown")
        raise DomainError("unauthorized", "Invalid or missing worker token.", 401)


def _reply(model: object) -> JSONResponse:
    response = ok(model.model_dump(mode="json"))  # type: ignore[attr-defined]
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/claim", dependencies=[Depends(require_worker)])
def claim(body: p.ClaimRequest, db: Session = Depends(get_db)) -> JSONResponse:
    answer = worker_service.claim(db, body)
    if answer.task is None:
        # Old workers parse ClaimResponse strictly (extra=forbid): never send them the q15 ``task`` key.
        response = ok(answer.model_dump(mode="json", exclude={"task"}))
        response.headers["Cache-Control"] = "no-store"
        return response
    return _reply(answer)


@router.post("/tasks/{task_id}/result", dependencies=[Depends(require_worker)])
def task_result(task_id: int, body: p.TaskResultRequest, db: Session = Depends(get_db)) -> JSONResponse:
    from sales0.services.research import linkedin_page

    return _reply(linkedin_page.record_result(db, task_id, body))


@router.post("/runs/{run_id}/heartbeat", dependencies=[Depends(require_worker)])
def heartbeat(run_id: int, body: p.HeartbeatRequest, db: Session = Depends(get_db)) -> JSONResponse:
    return _reply(worker_service.heartbeat(db, run_id, body))


@router.post("/runs/{run_id}/keywords/{row_id}/posts", dependencies=[Depends(require_worker)])
def upload_posts(
    run_id: int, row_id: int, body: p.UploadPostsRequest, db: Session = Depends(get_db), llm: LLMClient = Depends(get_llm)
) -> JSONResponse:
    return _reply(worker_service.upload_posts(db, run_id, row_id, body, llm=llm))


@router.post("/runs/{run_id}/keywords/{row_id}/links", dependencies=[Depends(require_worker)])
def upload_links(run_id: int, row_id: int, body: p.UploadLinksRequest, db: Session = Depends(get_db)) -> JSONResponse:
    return _reply(worker_service.upload_links(db, run_id, row_id, body))


@router.post("/runs/{run_id}/complete", dependencies=[Depends(require_worker)])
def complete(run_id: int, body: p.CompleteRequest, db: Session = Depends(get_db), llm: LLMClient = Depends(get_llm)) -> JSONResponse:
    return _reply(worker_service.complete(db, run_id, body, llm=llm))


def _require_hosted() -> None:
    if get_settings().mode != "hosted":
        raise DomainError("not_found", "That item no longer exists - refresh the page.", 404)


@router.post("/import/batch", dependencies=[Depends(require_worker), Depends(_require_hosted)])
def import_batch(body: p.ImportBatchRequest, db: Session = Depends(get_db)) -> JSONResponse:
    stats = data_import.import_batch(db, body.table, body.phase, body.rows, body.member_emails)
    return _reply(p.ImportBatchResponse(**stats))


@router.post("/import/deck-chunk", dependencies=[Depends(require_worker), Depends(_require_hosted)])
def import_deck_chunk(body: p.ImportDeckChunkRequest, db: Session = Depends(get_db)) -> JSONResponse:
    try:
        data = base64.b64decode(body.data_b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DomainError("deck_chunk_invalid", "The deck chunk is not valid base64.", 422) from exc
    stats = data_import.import_deck_chunk(
        db, deck_id=body.deck_id, sha256=body.sha256, offset=body.offset, total=body.total, data=data
    )  # fmt: skip
    return _reply(p.ImportDeckChunkResponse(**stats))


@router.post("/import/finalize", dependencies=[Depends(require_worker), Depends(_require_hosted)])
def import_finalize(body: p.ImportFinalizeRequest, db: Session = Depends(get_db)) -> JSONResponse:
    return _reply(p.ImportFinalizeResponse.model_validate(data_import.finalize(db, body.counts)))
