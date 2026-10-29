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
from sales0.web import cookies
from sales0.web.envelope import error_response, is_htmx, redact_path, unhandled_response

log = get_logger("sales0.http")

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


DEFAULT_BODY_LIMIT = 1024 * 1024  # 1 MB for every request body unless the path is a known upload
MULTIPART_OVERHEAD = 64 * 1024
UNSAFE_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def body_limit(method: str, path: str) -> tuple[int, str, str]:
    """(max body bytes, error code, message) for a request. Only the upload routes may exceed 1 MB."""
    from sales0.config import get_settings

    if method == "POST" and path == "/import/csv":
        return 5 * 1024 * 1024 + MULTIPART_OVERHEAD, "import_too_large", "That file is over 5 MB - split it and import the parts."
    if method == "POST" and path == "/outreach/decks":
        limit = get_settings().deck_max_bytes
        return limit + MULTIPART_OVERHEAD, "deck_too_large", f"That deck is over {limit // (1024 * 1024)} MB."
    if method == "POST" and path in ("/api/worker/import/batch", "/api/worker/import/deck-chunk"):
        return 8 * 1024 * 1024, "payload_too_large", "That import batch is over 8 MB."
    return DEFAULT_BODY_LIMIT, "payload_too_large", "That request is too large."


async def _input_summary(request: Request) -> dict[str, list[str]]:
    """Query keys and form field NAMES only - never values (passwords, PII).

    The body is read only when its Content-Length is declared and within the path's limit; a chunked body with no
    declared length is never buffered here (the endpoint reads it through the size-capped receive)."""
    summary: dict[str, list[str]] = {"query": sorted(set(request.query_params.keys()))}
    content_type = request.headers.get("content-type", "")
    if request.method in {"POST", "PUT", "PATCH"} and content_type.startswith("application/x-www-form-urlencoded"):
        declared = request.headers.get("content-length", "")
        limit = body_limit(request.method, request.url.path)[0]
        if declared.isdigit() and int(declared) <= limit:
            body = await request.body()  # cached by Starlette; the endpoint can still read it
            summary["form"] = sorted({k for k, _ in parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True)})
    elif content_type.startswith("multipart/form-data"):
        summary["form"] = ["<multipart>"]
    return summary


def security_headers(request_scheme_is_https: bool = False) -> list[tuple[bytes, bytes]]:
    """Headers added to EVERY response. CSP: scripts only from /static (no inline script or handlers; the one
    inline-style need - ``style=`` attributes - is confined to style-src-attr)."""
    from urllib.parse import urlsplit

    from sales0.config import get_settings

    settings = get_settings()
    form_targets = ["'self'"]
    google = urlsplit(settings.google_auth_endpoint)
    if google.scheme in {"http", "https"} and google.netloc:
        form_targets.append(f"{google.scheme}://{google.netloc}")  # Connect Gmail is a POST that redirects to Google
    csp = "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
            "style-src-attr 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            f"form-action {' '.join(form_targets)}",
            "frame-ancestors 'none'",
        ]
    )
    return [
        (b"content-security-policy", csp.encode()),
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"same-origin"),  # not no-referrer: browsers then send "Origin: null" on form POSTs, which the CSRF check refuses
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"),
    ]


class HardeningMiddleware:
    """Outermost pure-ASGI layer: (1) refuses an oversized body BEFORE anything reads it - by Content-Length, and
    for chunked bodies through a counting ``receive`` that raises 413 - and (2) stamps the security headers on every
    response, including errors raised by inner middleware."""

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        method, path = scope["method"].upper(), scope["path"]
        limit, code, message = body_limit(method, path)

        async def send_with_headers(msg) -> None:  # noqa: ANN001
            if msg["type"] == "http.response.start":
                present = {k.lower() for k, _ in msg.get("headers", [])}
                extra = [(k, v) for k, v in security_headers() if k not in present]
                if path.startswith(FIXTURE_PATH_PREFIX):
                    # The fake LinkedIn (fixture mode only, loopback only) ships inline scripts/styles that mimic the real site.
                    extra = [(k, v) for k, v in extra if k != b"content-security-policy"]
                msg = {**msg, "headers": [*msg.get("headers", []), *extra]}
            await send(msg)

        if method in UNSAFE_BODY_METHODS:
            raw_length = headers.get(b"content-length", b"").decode("latin-1").strip()
            if raw_length and not raw_length.isdigit():
                await self._reject(scope, receive, send_with_headers, "bad_request", "That request is not valid.", 400)
                return
            if raw_length and int(raw_length) > limit:
                log.warning("request_too_large", method=method, path=redact_path(path), declared_bytes=int(raw_length), limit=limit)
                await self._reject(scope, receive, send_with_headers, code, message, 413)
                return
            seen = 0
            overflow = False  # the streamed body went past the limit
            answered = False

            async def counting_receive():  # noqa: ANN202
                # Behind BaseHTTPMiddleware an exception raised here is wrapped in ExceptionGroups and surfaces as a
                # 400, so overflow is signalled as a disconnect and the 413 is written by this layer instead.
                nonlocal seen, overflow
                if overflow:
                    return {"type": "http.disconnect"}
                msg = await receive()
                if msg["type"] == "http.request":
                    seen += len(msg.get("body", b""))
                    if seen > limit:
                        overflow = True
                        log.warning("request_too_large", method=method, path=redact_path(path), limit=limit)
                        return {"type": "http.disconnect"}
                return msg

            async def guarded_send(msg) -> None:  # noqa: ANN001
                nonlocal answered
                if overflow:
                    if not answered:
                        answered = True
                        await self._reject(scope, receive, send_with_headers, code, message, 413)
                    return  # whatever the aborted handler tried to say is dropped
                await send_with_headers(msg)

            try:
                await self.app(scope, counting_receive, guarded_send)
            except Exception:
                if not overflow:
                    raise
            if overflow and not answered:
                await self._reject(scope, receive, send_with_headers, code, message, 413)
            return
        await self.app(scope, receive, send_with_headers)

    @staticmethod
    async def _reject(scope, receive, send, code: str, message: str, status: int) -> None:  # noqa: ANN001
        response = error_response(Request(scope, receive), code, message, status)
        await response(scope, receive, send)


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        bind_context(request_id=request_id)
        cookies.bind_request(request)
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
                path=redact_path(request.url.path),
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
            path=redact_path(request.url.path),
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
