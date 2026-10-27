"""Request-id binding + one ``http_request`` log line per request."""

from __future__ import annotations

import re
import time
import uuid
from urllib.parse import parse_qsl

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from sales0.logging import bind_context, get_logger, unbind_context
from sales0.web.envelope import is_htmx, unhandled_response

log = get_logger("sales0.http")

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


async def _input_summary(request: Request) -> dict[str, list[str]]:
    """Query keys and form field NAMES only — never values (passwords, PII)."""
    summary: dict[str, list[str]] = {"query": sorted(set(request.query_params.keys()))}
    content_type = request.headers.get("content-type", "")
    if request.method in {"POST", "PUT", "PATCH"} and content_type.startswith("application/x-www-form-urlencoded"):
        body = await request.body()  # cached by Starlette; the endpoint can still read it
        summary["form"] = sorted({k for k, _ in parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True)})
    elif content_type.startswith("multipart/form-data"):
        summary["form"] = ["<multipart>"]
    return summary


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        bind_context(request_id=request_id)
        started = time.perf_counter()
        try:
            input_summary = await _input_summary(request)
            try:
                response = await call_next(request)
            except Exception as exc:  # noqa: BLE001 — logged with exc_info, generic message to the user
                response = unhandled_response(request, exc)
            response.headers["X-Request-ID"] = request_id
            log.info(
                "http_request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                htmx=is_htmx(request),
                input_summary=input_summary,
                output_summary={
                    "content_type": response.headers.get("content-type"),
                    "bytes": int(response.headers["content-length"]) if "content-length" in response.headers else None,
                },
            )
            return response
        finally:
            unbind_context("request_id")
