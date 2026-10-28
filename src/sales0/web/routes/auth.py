"""Login / logout, one-time set-password links, own-password change (``spec/api.md`` -> Login, Accounts)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from sales0.config import Settings
from sales0.db.engine import get_db
from sales0.db.models import TeamMember
from sales0.errors import DomainError
from sales0.logging import get_logger
from sales0.services.team import accounts
from sales0.web import auth
from sales0.web.acting import clear_flash, get_login_member_optional, pop_flash, set_flash
from sales0.web.templating import get_templates

log = get_logger("sales0.auth")
router = APIRouter()

NO_STORE = {"Cache-Control": "no-store"}
WRONG = "Wrong email or password."


def _settings(request: Request) -> Settings:
    return request.app.state.auth_settings  # set by auth.install


def _page(
    request: Request, template: str, ctx: dict, *, status: int = 200, headers: dict | None = None
) -> Response:
    return get_templates().TemplateResponse(
        request, template, ctx, status_code=status, headers={**NO_STORE, **(headers or {})}
    )


def _login_page(request: Request, next_path: str, *, email: str = "", error: str | None = None, status: int = 200, headers: dict | None = None, remember: bool = False) -> Response:
    flash = pop_flash(request)
    response = _page(
        request, "auth/login.html",
        {"next_path": next_path, "error": error, "email": email, "remember": remember, "flash": flash},
        status=status, headers=headers,
    )
    if flash:
        clear_flash(response)
    return response


@router.get("/login", include_in_schema=False)
async def login_page(request: Request, next: str | None = None, db: Session = Depends(get_db)) -> Response:
    settings = _settings(request)
    if not accounts.auth_enabled(db, settings):
        return RedirectResponse("/", status_code=303)
    target = auth.safe_next(next)
    if getattr(request.state, "login_member_id", None):
        return RedirectResponse(target, status_code=303)
    return _login_page(request, target)


@router.post("/login", include_in_schema=False)
async def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    remember: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    settings = _settings(request)
    if not accounts.auth_enabled(db, settings):
        return RedirectResponse("/", status_code=303)
    guard: auth.LoginGuard = request.app.state.login_throttle
    target = auth.safe_next(next)
    ip = auth.client_ip(request, settings)
    moment = auth.now()
    email = email.strip().lower()
    wants_remember = remember.lower() in {"1", "on", "true", "yes"}

    left = guard.check(ip, email, moment)
    if left is not None:
        log.warning("login_locked", client=ip, retry_after_s=left)
        return _login_page(
            request, target, email=email, remember=wants_remember, status=429,
            error=f"Too many attempts - try again in {left} seconds.", headers={"Retry-After": str(left)},
        )

    member = accounts.authenticate(db, email, password, settings, moment)
    if member is None:
        guard.fail(ip, email, moment)
        log.warning("login_failed", client=ip)
        return _login_page(request, target, email=email, remember=wants_remember, error=WRONG, status=401)

    guard.reset(ip, email)
    sid, expires = accounts.create_session(db, member, settings, remember=wants_remember, ip=ip, now=moment)
    db.commit()
    log.info("login_ok", client=ip, member_id=member.id, remember=wants_remember)
    response = RedirectResponse(target, status_code=303, headers=NO_STORE)
    auth.set_session_cookie(response, settings, sid, expires, moment)
    return response


@router.post("/logout", include_in_schema=False)
async def logout(request: Request, db: Session = Depends(get_db)) -> Response:
    settings = _settings(request)
    accounts.revoke_session(db, auth.cookie_sid(request, settings))
    response = RedirectResponse("/login" if accounts.auth_enabled(db, settings) else "/", status_code=303, headers=NO_STORE)
    auth.clear_session(response, settings)
    return response


# ---------------------------------------------------------------------- set-password links
def _token_guard(request: Request) -> tuple[auth.LoginGuard, str]:
    return request.app.state.login_throttle, auth.client_ip(request, _settings(request))


def _set_password_page(request: Request, token: str, member: TeamMember | None, *, error: str | None = None, status: int = 200) -> Response:
    return _page(
        request, "auth/set_password.html",
        {"token": token, "member": member, "error": error, "min_length": _settings(request).password_min_length},
        status=status if member is not None else 410,
    )


@router.get("/set-password/{token}", include_in_schema=False)
async def set_password_page(request: Request, token: str, db: Session = Depends(get_db)) -> Response:
    guard, ip = _token_guard(request)
    left = guard.check(ip, "set-password", auth.now())
    if left is not None:
        return _set_password_page(request, token, None, error=f"Too many attempts - try again in {left} seconds.", status=429)
    member = accounts.peek_token(db, token, auth.now())
    if member is None:
        guard.fail(ip, "set-password", auth.now())
    return _set_password_page(request, token, member)


@router.post("/set-password/{token}", include_in_schema=False)
async def set_password_submit(
    request: Request,
    token: str,
    password: str = Form(""),
    confirm: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    settings = _settings(request)
    guard, ip = _token_guard(request)
    left = guard.check(ip, "set-password", auth.now())
    if left is not None:
        return _set_password_page(request, token, None, error=f"Too many attempts - try again in {left} seconds.", status=429)
    member = accounts.peek_token(db, token, auth.now())
    if member is None:
        guard.fail(ip, "set-password", auth.now())
        return _set_password_page(request, token, None)
    if password != confirm:
        return _set_password_page(request, token, member, error="The two passwords do not match.", status=400)
    try:
        accounts.set_password_with_token(db, token, password, settings, auth.now())
    except DomainError as exc:
        return _set_password_page(request, token, member, error=exc.message, status=400)
    db.commit()
    response = RedirectResponse("/login", status_code=303, headers=NO_STORE)
    auth.clear_session(response, settings)
    set_flash(response, "Password set - log in with your email and the new password.")
    return response


# ---------------------------------------------------------------------- own account
@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, db: Session = Depends(get_db), me: TeamMember | None = Depends(get_login_member_optional)) -> Response:
    from sales0.web.context import render_page

    flash = pop_flash(request)
    response = render_page(
        request, db, "account/page.html",
        {"me": me, "flash": flash, "min_length": _settings(request).password_min_length},
    )
    if flash:
        clear_flash(response)
    return response


@router.post("/account/password", include_in_schema=False)
async def change_password(
    request: Request,
    current: str = Form(""),
    password: str = Form(""),
    confirm: str = Form(""),
    db: Session = Depends(get_db),
    me: TeamMember | None = Depends(get_login_member_optional),
) -> Response:
    settings = _settings(request)
    if me is None:
        raise DomainError("forbidden", "Log in first - passwords are not enabled yet.", 403)
    response = RedirectResponse("/account", status_code=303)
    guard: auth.LoginGuard = request.app.state.login_throttle
    ip, key, moment = auth.client_ip(request, settings), f"{me.email}#password-change", auth.now()
    left = guard.check(ip, key, moment)
    if left is not None:  # the current password is a guessable secret: same lockout as the login form
        log.warning("password_change_locked", client=ip, member_id=me.id, retry_after_s=left)
        set_flash(response, f"Too many attempts - try again in {left} seconds.", "error")
        response.headers["Retry-After"] = str(left)
        return response
    if password != confirm:
        set_flash(response, "The two new passwords do not match.", "error")
        return response
    try:
        accounts.change_own_password(
            db, me, current, password, settings, keep_session_id=getattr(request.state, "login_session_id", None), now=moment
        )
    except DomainError as exc:
        if exc.code == "password_wrong":
            guard.fail(ip, key, moment)
        set_flash(response, exc.message, "error")
        return response
    guard.reset(ip, key)
    set_flash(response, "Password changed. Your other sessions were signed out.")
    return response
