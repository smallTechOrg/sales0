"""Admin Users page: list, add, role, activate/deactivate, one-time set-password links (``spec/api.md`` -> Accounts)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.engine import get_db
from sales0.db.models import TeamMember
from sales0.services.team import accounts
from sales0.services.team import members as members_service
from sales0.web import auth
from sales0.web.acting import clear_flash, pop_flash, require_admin, set_flash
from sales0.web.context import PathId, render_page

router = APIRouter()


def _link_url(request: Request, token: str) -> str:
    settings = get_settings()
    base = (settings.public_base_url or str(request.base_url)).rstrip("/")
    return f"{base}/set-password/{token}"


def _render(request: Request, db: Session, admin: TeamMember, extra: dict[str, Any] | None = None) -> Response:
    flash = pop_flash(request)
    rows = members_service.list_members(db)
    ctx = {
        "rows": rows,
        "admin": admin,
        "flash": flash,
        "link": None,
        "open_mode": not bool(getattr(request.state, "auth_enabled", False)),
        "ttl_h": get_settings().password_reset_ttl_h,
        **(extra or {}),
    }
    response = render_page(request, db, "users/page.html", ctx, headers={"Cache-Control": "no-store"})
    if flash:
        clear_flash(response)
    return response


def _back(message: str, kind: str = "ok") -> Response:
    response = RedirectResponse("/users", status_code=303)
    set_flash(response, message, kind)
    return response


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_db), admin: TeamMember = Depends(require_admin)) -> Response:
    return _render(request, db, admin)


@router.post("/users")
def add_user(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    role: str = Form("member"),
    db: Session = Depends(get_db),
    admin: TeamMember = Depends(require_admin),
) -> Response:
    member = members_service.add_member(db, name, email, role)
    raw, expires = accounts.issue_set_password_link(db, member, admin, get_settings(), auth.now())
    return _render(request, db, admin, {"link": {"member": member, "url": _link_url(request, raw), "expires": expires, "first": True}})


@router.post("/users/{member_id}/reset")
def reset_link(
    request: Request,
    member_id: PathId,
    db: Session = Depends(get_db),
    admin: TeamMember = Depends(require_admin),
) -> Response:
    member = members_service.get_member(db, member_id)
    raw, expires = accounts.issue_set_password_link(db, member, admin, get_settings(), auth.now())
    return _render(request, db, admin, {"link": {"member": member, "url": _link_url(request, raw), "expires": expires, "first": False}})


@router.post("/users/{member_id}/role")
def change_role(
    member_id: PathId, role: str = Form(...), db: Session = Depends(get_db), admin: TeamMember = Depends(require_admin)
) -> Response:
    member = members_service.set_role(db, member_id, role, by=admin)
    return _back(f"{member.name} is now {member.role}.")


@router.post("/users/{member_id}/active")
def change_active(
    member_id: PathId, active: str = Form(...), db: Session = Depends(get_db), admin: TeamMember = Depends(require_admin)
) -> Response:
    member = members_service.set_active(db, member_id, active == "1", by=admin)
    return _back(f"{member.name} is now {'active' if member.active else 'inactive - signed out everywhere'}.")

