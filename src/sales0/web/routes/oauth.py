"""Team page, "acting as" selector and the Google OAuth connect/callback for per-member Gmail.

The callback is a cross-site GET from Google, so CsrfOriginMiddleware (unsafe methods only)
does not touch it; its CSRF defence is the single-use ``state`` bound to the acting member.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from sales0.db.engine import get_db
from sales0.db.models import TeamMember
from sales0.errors import DomainError
from sales0.integrations.gmail.errors import GmailError
from sales0.logging import get_logger
from sales0.services.team import connections as connections_service
from sales0.services.team import members as members_service
from sales0.services.team import oauth as oauth_service
from sales0.web.acting import (
    clear_flash,
    get_acting_member,
    get_acting_member_optional,
    pop_flash,
    set_acting_cookie,
    set_flash,
)
from sales0.web.context import render_page

log = get_logger("sales0.team.web")
router = APIRouter()


def oauth_http(request: Request) -> Iterator[httpx.Client]:
    """httpx client for Google calls. Tests set ``app.state.oauth_http_factory`` to return a
    client over ``httpx.MockTransport``."""
    factory = getattr(request.app.state, "oauth_http_factory", None)
    client = factory() if factory else httpx.Client(timeout=20)
    try:
        yield client
    finally:
        client.close()


def _back(message: str, kind: str = "ok") -> RedirectResponse:
    response = RedirectResponse("/team", status_code=303)
    set_flash(response, message, kind)
    return response


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/team"


@router.get("/team", response_class=HTMLResponse)
def team_page(
    request: Request,
    db: Session = Depends(get_db),
    acting: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    rows: list[dict[str, Any]] = [
        {"member": m, "connection": connections_service.connection_status(db, m.id)}
        for m in members_service.list_members(db)
    ]
    flash = pop_flash(request)
    response = render_page(request, db, "team/page.html", {"rows": rows, "flash": flash})
    if flash:
        clear_flash(response)
    return response


@router.post("/team/acting")
def switch_acting(
    member_id: int = Form(...), next: str = Form("/team"), db: Session = Depends(get_db)
) -> Response:
    member = db.get(TeamMember, member_id)
    if member is None or not member.active:
        raise DomainError("not_found", "That team member is not available - refresh the page.", 404)
    response = RedirectResponse(_safe_next(next), status_code=303)
    set_acting_cookie(response, member.id)
    return response


@router.post("/team/members")
def add_member(
    name: str = Form(...),
    email: str = Form(...),
    role: str = Form("member"),
    db: Session = Depends(get_db),
    acting: TeamMember = Depends(get_acting_member),
) -> Response:
    if acting.role != "admin":
        raise DomainError("forbidden", "Only an admin can add team members.", 403)
    member = members_service.add_member(db, name, email, role)
    return _back(f"Added {member.name}. They can now switch to themselves and connect their Gmail.")


@router.post("/team/members/{member_id}/active")
def set_member_active(
    member_id: int,
    active: str = Form(...),
    db: Session = Depends(get_db),
    acting: TeamMember = Depends(get_acting_member),
) -> Response:
    if acting.role != "admin":
        raise DomainError("forbidden", "Only an admin can change who is active.", 403)
    member = members_service.set_active(db, member_id, active == "1")
    return _back(f"{member.name} is now {'active' if member.active else 'inactive'}.")


@router.post("/team/gmail/connect")
def connect_gmail(
    request: Request, acting: TeamMember = Depends(get_acting_member)
) -> Response:
    try:
        redirect_uri = oauth_service.redirect_uri_for_request(str(request.url), request.headers.get("host", ""))
        pending = oauth_service.STORE.create(acting.id, redirect_uri)
        url = oauth_service.build_auth_url(pending, acting.email)
    except DomainError as exc:
        return _back(exc.message, "error")
    except GmailError as exc:
        return _back(str(exc), "error")
    return RedirectResponse(url, status_code=303)


@router.post("/team/gmail/disconnect")
def disconnect_gmail(
    member_id: int = Form(...),
    db: Session = Depends(get_db),
    acting: TeamMember = Depends(get_acting_member),
    http: httpx.Client = Depends(oauth_http),
) -> Response:
    if acting.id != member_id and acting.role != "admin":
        raise DomainError("forbidden", "You can only disconnect your own Gmail (admins can disconnect anyone's).", 403)
    member = members_service.get_member(db, member_id)
    connections_service.disconnect(db, member_id, http=http)
    return _back(f"Gmail disconnected for {member.name}.")


@router.get("/oauth/google/callback")
def google_callback(
    request: Request,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
    acting: TeamMember | None = Depends(get_acting_member_optional),
    http: httpx.Client = Depends(oauth_http),
) -> Response:
    try:
        pending = oauth_service.STORE.consume(state, acting.id if acting else None)
    except DomainError as exc:
        return _back(exc.message, "error")
    if error:
        if error == "access_denied":
            return _back("Google sign-in was cancelled - Gmail was not connected.", "error")
        return _back(f"Google reported an error ({error[:60]}). Gmail was not connected.", "error")
    if not code:
        return _back("Google did not send a sign-in code. Press Connect my Gmail to try again.", "error")
    try:
        token = oauth_service.exchange_code(code, pending, http)
        view = connections_service.store_token(db, pending.member_id, token)
    except DomainError as exc:
        return _back(exc.message, "error")
    except GmailError as exc:
        return _back(str(exc), "error")
    if view.status != "connected":
        return _back(view.last_error or "Gmail connected with missing permissions.", "error")
    return _back(f"Gmail connected as {view.email}.")


