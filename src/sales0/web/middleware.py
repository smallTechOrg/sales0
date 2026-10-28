"""Request-id binding + one ``http_request`` log line per request; CSRF Origin/Referer check."""

from __future__ import annotations

import re
import time
import uuid
from urllib.parse import parse_qsl, urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from sales0.logging import bind_context, get_logger, unbind_context
from sales0.web.envelope import error_response, is_htmx, unhandled_response

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


# ---------------------------------------------------------------------- CSRF (Origin / Referer check)
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
FIXTURE_PATH_PREFIX = "/__fixtures/"
CSRF_MESSAGE = "That request came from another website and was blocked. Use sales0 from its own tab."
_DEFAULT_PORTS = {"http": 80, "https": 443}

Origin = tuple[str, str, int]


def _parse_origin(value: str) -> Origin | None:
    """``scheme://host[:port]`` (or a full URL) → (scheme, host, port); None if unparseable/opaque."""
    try:
        parts = urlsplit(value.strip())
        scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    return scheme, host, port if port is not None else _DEFAULT_PORTS[scheme]


def allowed_origins(request: Request) -> set[Origin]:
    """The server's own origins: configured host/port, loopback aliases, and the Host header
    the request actually arrived on — the latter only when it names a loopback/configured host,
    so a DNS-rebinding page (Host = attacker domain) is not treated as same-origin."""
    from sales0.config import LOOPBACK_HOSTS, get_settings

    settings = get_settings()
    trusted_hosts = {h.lower() for h in LOOPBACK_HOSTS} | {settings.host.lower()}
    ports = {settings.port}
    origins: set[Origin] = set()
    host_header = request.headers.get("host", "")
    if host_header:
        own = _parse_origin(f"{request.url.scheme}://{host_header}")
        if own and own[1].strip("[]") in trusted_hosts:
            origins.add(own)
            ports.add(own[2])  # the port actually served (tests / --port overrides)
    public = _parse_origin(settings.public_base_url) if settings.public_base_url else None
    if public:
        origins.add(public)  # a VPS behind a domain: the browser's Origin is the public URL
    for host in trusted_hosts:
        for port in ports:
            parsed = _parse_origin(f"http://{'[' + host + ']' if ':' in host else host}:{port}")
            if parsed:
                origins.add(parsed)
    return origins


def csrf_rejection_reason(request: Request) -> str | None:
    """None when the request may proceed; otherwise which header failed the check."""
    if request.method.upper() not in UNSAFE_METHODS:
        return None
    if request.url.path.startswith(FIXTURE_PATH_PREFIX):
        from sales0.config import get_settings

        if get_settings().linkedin_mode == "fixture":
            return None
    origin = request.headers.get("origin")
    if origin is not None:
        return None if _parse_origin(origin) in allowed_origins(request) else "origin"
    referer = request.headers.get("referer")
    if referer is not None:
        return None if _parse_origin(referer) in allowed_origins(request) else "referer"
    return None  # no browser provenance headers: a non-browser client (curl, TestClient)


class CsrfOriginMiddleware(BaseHTTPMiddleware):
    """Blocks cross-site POST/PUT/PATCH/DELETE (any website the user visits could otherwise
    start a LinkedIn run on 127.0.0.1). Runs inside RequestContextMiddleware so rejections
    carry a request id and the usual ``http_request`` log line."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        reason = csrf_rejection_reason(request)
        if reason is None:
            return await call_next(request)
        log.warning(
            "csrf_rejected",
            method=request.method,
            path=request.url.path,
            failed_header=reason,
            origin=request.headers.get("origin"),
            referer_origin=_origin_text(request.headers.get("referer")),
        )
        return error_response(request, "csrf_rejected", CSRF_MESSAGE, 403)


def _origin_text(value: str | None) -> str | None:
    """Log only the origin part of a Referer — never its path/query."""
    if not value:
        return None
    parsed = _parse_origin(value)
    return f"{parsed[0]}://{parsed[1]}:{parsed[2]}" if parsed else "<unparseable>"
