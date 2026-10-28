"""Login / logout pages (``spec/api.md`` → Login)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from sales0.config import Settings
from sales0.logging import get_logger
from sales0.web import auth
from sales0.web.templating import get_templates

log = get_logger("sales0.auth")
router = APIRouter()

NO_STORE = {"Cache-Control": "no-store"}


def _settings(request: Request) -> Settings:
    return request.app.state.auth_settings  # set by auth.install


def _page(request: Request, next_path: str, *, error: str | None = None, status: int = 200, headers: dict | None = None) -> Response:
    return get_templates().TemplateResponse(
        request,
        "auth/login.html",
        {"next_path": next_path, "error": error},
        status_code=status,
        headers={**NO_STORE, **(headers or {})},
    )


@router.get("/login", include_in_schema=False)
async def login_page(request: Request, next: str | None = None) -> Response:
    settings = _settings(request)
    if not settings.app_password_set:
        return RedirectResponse("/", status_code=303)
    target = auth.safe_next(next)
    if auth.session_valid(request, settings):
        return RedirectResponse(target, status_code=303)
    return _page(request, target)


@router.post("/login", include_in_schema=False)
async def login_submit(request: Request, password: str = Form(""), next: str = Form("")) -> Response:
    settings = _settings(request)
    if not settings.app_password_set:
        return RedirectResponse("/", status_code=303)
    throttle: auth.LoginThrottle = request.app.state.login_throttle
    target = auth.safe_next(next)
    ip = auth.client_ip(request)
    moment = auth.now()

    left = throttle.check(ip, moment)
    if left is not None:
        log.warning("login_locked", client=ip, retry_after_s=left)
        return _page(request, target, error=f"Too many attempts - try again in {left} seconds.", status=429, headers={"Retry-After": str(left)})

    if not auth.password_matches(settings, password):
        throttle.fail(ip, moment)
        log.warning("login_failed", client=ip)
        return _page(request, target, error="Wrong password.", status=401)

    throttle.reset(ip)
    log.info("login_ok", client=ip)
    response = RedirectResponse(target, status_code=303, headers=NO_STORE)
    auth.issue_session(response, settings, moment)
    return response


@router.post("/logout", include_in_schema=False)
async def logout(request: Request) -> Response:
    settings = _settings(request)
    response = RedirectResponse("/login" if settings.app_password_set else "/", status_code=303, headers=NO_STORE)
    auth.clear_session(response, settings)
    return response
