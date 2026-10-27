"""Response envelope + exception handlers.

JSON (``/api/*`` or ``Accept: application/json``): ``{"ok": true, "data": ...}`` or
``{"ok": false, "error": {"code", "message"}}``. htmx requests get an HTTP 200 toast
fragment retargeted at ``#toasts``. Full-page requests render ``error.html``.
"""

from __future__ import annotations

from html import escape
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from sales0.errors import DomainError
from sales0.logging import get_logger

log = get_logger("sales0.web")

GENERIC_MESSAGE = "Something went wrong — details are in the server log (request_id={request_id})."

_HTTP_CODES = {
    400: ("bad_request", "The request was not valid."),
    401: ("unauthorized", "Please log in."),
    403: ("forbidden", "You are not allowed to do that."),
    404: ("not_found", "That item no longer exists — refresh the page."),
    405: ("method_not_allowed", "That action is not supported here."),
    409: ("conflict", "That conflicts with the current state — refresh the page."),
    429: ("rate_limited", "Too many requests — wait a moment and try again."),
}


def ok(data: Any = None, status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True, "data": jsonable_encoder(data)}, status_code=status)


def err(code: str, message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": {"code": code, "message": message}}, status_code=status)


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def wants_json(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def toast(message: str, kind: str = "error") -> HTMLResponse:
    role = "alert" if kind == "error" else "status"
    html = f'<div class="toast toast--{kind}" role="{role}">{escape(message)}</div>'
    return HTMLResponse(html, status_code=200, headers={"HX-Retarget": "#toasts", "HX-Reswap": "beforeend"})


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "-")


def error_response(request: Request, code: str, message: str, status: int) -> Response:
    """Render an error in the shape the caller expects (JSON, htmx toast, or full page)."""
    if wants_json(request):
        return err(code, message, status)
    if is_htmx(request):
        return toast(message)
    from sales0.web.templating import get_templates

    try:
        return get_templates().TemplateResponse(
            request,
            "error.html",
            {"status": status, "code": code, "message": message, "back_href": "/"},
            status_code=status,
        )
    except Exception:  # noqa: BLE001 — never let the error page itself crash
        log.exception("error_page_render_failed", code=code)
        return HTMLResponse(f"<h1>Error</h1><p>{escape(message)}</p><p><a href='/'>Back</a></p>", status_code=status)


def unhandled_response(request: Request, exc: BaseException) -> Response:
    log.error("unhandled_exception", path=request.url.path, error_type=type(exc).__name__, exc_info=exc)
    return error_response(request, "internal_error", GENERIC_MESSAGE.format(request_id=_request_id(request)), 500)


async def _domain_error_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, DomainError)
    log.info("domain_error", code=exc.code, http_status=exc.http_status, path=request.url.path)
    return error_response(request, exc.code, exc.message, exc.http_status)


async def _http_exception_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    code, default_message = _HTTP_CODES.get(exc.status_code, ("http_error", "The request could not be completed."))
    message = exc.detail if isinstance(exc.detail, str) and exc.status_code not in _HTTP_CODES else default_message
    response = error_response(request, code, message, exc.status_code)
    if exc.headers:
        response.headers.update(exc.headers)
    return response


async def _validation_error_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    fields = sorted({".".join(str(p) for p in e.get("loc", ())[1:]) or "body" for e in exc.errors()})
    message = f"Some fields are missing or invalid: {', '.join(fields)}."
    return error_response(request, "validation_error", message, 422)


async def _unhandled_handler(request: Request, exc: Exception) -> Response:
    return unhandled_response(request, exc)


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_handler)
