"""Criteria page (inbox feedback A3): the triage include / exclude rules, transparent and editable, plus a breakdown
of discard reasons (team / system / AI) to fine-tune them.

Everyone can read the page; changing a rule is admin-only (403 ``forbidden`` for members, and the page hides the
controls they cannot use). Conventions as in ``registries.py`` (htmx -> redirect + flash, plain forms -> 303 + flash,
JSON -> envelope).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from sales0.db.engine import get_db
from sales0.db.models import TeamMember
from sales0.errors import DomainError
from sales0.services import criteria as criteria_service
from sales0.web.acting import clear_flash, get_acting_member_optional, pop_flash, require_admin
from sales0.web.context import PathId, crm_done, crm_guard, render_page
from sales0.web.envelope import ok as envelope_ok
from sales0.web.envelope import wants_json

router = APIRouter()
BACK = "/criteria"


def _member_id(member: TeamMember | None, admin: TeamMember) -> int:
    return member.id if member is not None else admin.id


def _is_admin(request: Request, db: Session) -> bool:
    try:
        require_admin(request, db)
    except DomainError:
        return False
    return True


@router.get("/criteria", response_class=HTMLResponse)
def criteria_page(request: Request, db: Session = Depends(get_db)) -> Response:
    rules = criteria_service.list_rules(db)
    breakdown = criteria_service.discard_breakdown(db)
    active = criteria_service.active_set(db)
    if wants_json(request):
        return envelope_ok({
            "version": active.version,
            "rules": [asdict(r) | {"created_at": r.created_at.isoformat(), "updated_at": r.updated_at.isoformat()} for r in rules],
        })
    flash = pop_flash(request)
    ctx: dict[str, Any] = {
        "include": [r for r in rules if r.kind == "include"],
        "exclude": [r for r in rules if r.kind == "exclude"],
        "version": active.version,
        "history": criteria_service.history(db),
        "breakdown": breakdown,
        "can_edit": _is_admin(request, db),
        "flash": flash,
    }
    response = render_page(request, db, "criteria/page.html", ctx)
    if flash:
        clear_flash(response)
    return response


@router.post("/criteria", response_model=None)
def create_rule(
    request: Request,
    kind: str = Form(""),
    text: str = Form(""),
    db: Session = Depends(get_db),
    admin: TeamMember = Depends(require_admin),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def run() -> Response:
        view = criteria_service.create_rule(db, kind, text, actor_member_id=_member_id(member, admin))
        return crm_done(request, BACK, f"Added {view.kind} rule.", {"id": view.id})

    return crm_guard(request, BACK, run, db)


@router.post("/criteria/{criterion_id}", response_model=None)
def update_rule(
    request: Request,
    criterion_id: PathId,
    text: str = Form(""),
    kind: str = Form(""),
    db: Session = Depends(get_db),
    admin: TeamMember = Depends(require_admin),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def run() -> Response:
        criteria_service.update_rule(
            db, criterion_id, text=text if text.strip() else None, kind=kind or None,
            actor_member_id=_member_id(member, admin),
        )
        return crm_done(request, BACK, "Rule saved.")

    return crm_guard(request, BACK, run, db)


@router.post("/criteria/{criterion_id}/active", response_model=None)
def set_active(
    request: Request,
    criterion_id: PathId,
    active: str = Form("true"),
    db: Session = Depends(get_db),
    admin: TeamMember = Depends(require_admin),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    on = active.strip().lower() in ("1", "true", "on", "yes")

    def run() -> Response:
        criteria_service.set_active(db, criterion_id, on, actor_member_id=_member_id(member, admin))
        return crm_done(request, BACK, "Rule enabled." if on else "Rule disabled.")

    return crm_guard(request, BACK, run, db)


@router.post("/criteria/{criterion_id}/move", response_model=None)
def move_rule(
    request: Request,
    criterion_id: PathId,
    direction: str = Form(""),
    db: Session = Depends(get_db),
    admin: TeamMember = Depends(require_admin),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def run() -> Response:
        criteria_service.move(db, criterion_id, direction, actor_member_id=_member_id(member, admin))
        return crm_done(request, BACK, "Rule moved.")

    return crm_guard(request, BACK, run, db)


__all__ = ["router"]
