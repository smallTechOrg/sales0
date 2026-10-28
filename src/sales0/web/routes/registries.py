"""Competitors & Groups registries (Phase 2): CRUD for the app-owned rows; rows fed by the
Google Sheet show their sheet-owned fields read-only (only notes and Active can be changed here,
and Delete answers ``sheet_owned``).

Conventions as in ``pipeline.py`` (htmx -> redirect + flash, plain forms -> 303 + flash, JSON -> envelope).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from sales0.db.engine import get_db
from sales0.db.models import TeamMember
from sales0.errors import DomainError
from sales0.services.crm import clients as clients_service
from sales0.services.crm import registries as registries_service
from sales0.web.acting import clear_flash, get_acting_member_optional, pop_flash
from sales0.web.context import actor_of, crm_done, crm_guard, opt_int, opt_str, render_page
from sales0.web.envelope import ok as envelope_ok
from sales0.web.envelope import wants_json

router = APIRouter()

PLATFORMS = ("whatsapp", "linkedin", "telegram", "facebook", "other")
BACK = "/competitors"


def _dump(data: Any) -> Any:
    return data.model_dump(mode="json") if hasattr(data, "model_dump") else data


def _flag(values: list[str], default: bool = True) -> bool:
    """A checkbox posted after a hidden 'false' field: the last value wins; absent = default."""
    if not values:
        return default
    return values[-1].strip().lower() in ("true", "1", "on", "yes")


def _page(request: Request, db: Session, template: str, ctx: dict[str, Any]) -> HTMLResponse:
    flash = pop_flash(request)
    response = render_page(request, db, template, {**ctx, "flash": flash})
    if flash:
        clear_flash(response)
    return response


@router.get("/competitors", response_class=HTMLResponse)
def registries_page(request: Request, q: str = "", db: Session = Depends(get_db)) -> Response:
    query = q.strip()[:200]
    competitors = registries_service.list_competitors(db, q=query or None)
    groups = registries_service.list_groups(db)
    if wants_json(request):
        return envelope_ok({"competitors": [_dump(c) for c in competitors], "groups": [_dump(g) for g in groups]})
    return _page(request, db, "registries/page.html", {"competitors": competitors, "groups": groups, "q": query, "platforms": PLATFORMS})


@router.get("/competitors/competitor/{competitor_id}", response_class=HTMLResponse)
def competitor_page(request: Request, competitor_id: int, db: Session = Depends(get_db)) -> Response:
    competitor = registries_service.get_competitor(db, competitor_id)
    if wants_json(request):
        return envelope_ok(_dump(competitor))
    linked = {c.id for c in competitor.clients}
    choices = [c for c in clients_service.list_clients(db, page_size=500).items if c.id not in linked]
    return _page(request, db, "registries/competitor.html", {"c": competitor, "client_choices": choices})


# ---------------------------------------------------------------------- competitors
@router.post("/competitors/competitor", response_model=None)
def create_competitor(
    request: Request,
    name: str = Form(""),
    website: str = Form(""),
    approx_clients: str = Form(""),
    client_names: str = Form(""),
    contact: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def run() -> Response:
        view = registries_service.create_competitor(
            db, name=name.strip(), website=opt_str(website), approx_clients=opt_int(approx_clients, "client count"),
            client_names=opt_str(client_names), contact=opt_str(contact), email=opt_str(email), notes=opt_str(notes),
            actor=actor_of(member),
        )  # fmt: skip
        if wants_json(request):
            return envelope_ok(_dump(view), 201)
        return crm_done(request, BACK, f"Added competitor {view.name}.")

    return crm_guard(request, BACK, run, db)


@router.post("/competitors/competitor/{competitor_id}", response_model=None)
def update_competitor(
    request: Request,
    competitor_id: int,
    name: str = Form(""),
    website: str = Form(""),
    approx_clients: str = Form(""),
    client_names: str = Form(""),
    contact: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
    active: list[str] = Form(default_factory=list),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def run() -> Response:
        current = registries_service.get_competitor(db, competitor_id)
        fields: dict[str, Any] = {"notes": opt_str(notes), "active": _flag(active, current.active)}
        if not current.sheet_synced:  # the sheet owns the rest of a sheet-fed row
            if not name.strip():
                raise DomainError("competitor_invalid", "A competitor needs a name.", 400)
            fields.update(
                name=name.strip(), website=opt_str(website), approx_clients=opt_int(approx_clients, "client count"),
                client_names=opt_str(client_names), contact=opt_str(contact), email=opt_str(email),
            )  # fmt: skip
        view = registries_service.update_competitor(db, competitor_id, actor=actor_of(member), **fields)
        if wants_json(request):
            return envelope_ok(_dump(view))
        return crm_done(request, BACK, f"Saved {view.name}.")

    return crm_guard(request, BACK, run, db)


@router.post("/competitors/competitor/{competitor_id}/delete", response_model=None)
def delete_competitor(
    request: Request,
    competitor_id: int,
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def run() -> Response:
        registries_service.delete_competitor(db, competitor_id, actor=actor_of(member))
        if wants_json(request):
            return envelope_ok({"deleted": competitor_id})
        return crm_done(request, BACK, "Competitor deleted.")

    return crm_guard(request, BACK, run, db)


@router.post("/competitors/competitor/{competitor_id}/link", response_model=None)
def link_competitor(
    request: Request,
    competitor_id: int,
    entity_type: str = Form(""),
    entity_id: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    back = f"/competitors/competitor/{competitor_id}"

    def run() -> Response:
        if entity_type not in ("enquiry", "client"):
            raise DomainError("bad_request", "Choose an enquiry or a client to link.", 400)
        target = opt_int(entity_id, "selection")
        if target is None:
            raise DomainError("bad_request", "Choose what to link.", 400)
        registries_service.link_competitor(db, competitor_id, entity_type, target, actor=actor_of(member))
        if wants_json(request):
            return envelope_ok({"linked": [entity_type, target]})
        return crm_done(request, back, f"Linked the {entity_type}.")

    return crm_guard(request, back, run, db)


# ---------------------------------------------------------------------- groups
@router.post("/competitors/group", response_model=None)
def create_group(
    request: Request,
    name: str = Form(""),
    platform: str = Form("whatsapp"),
    link: str = Form(""),
    details: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def run() -> Response:
        if not name.strip():
            raise DomainError("group_invalid", "A group needs a name.", 400)
        view = registries_service.create_group(
            db, name=name.strip(), platform=(platform or "whatsapp").strip().lower(), link=opt_str(link),
            details=opt_str(details), notes=opt_str(notes), actor=actor_of(member),
        )  # fmt: skip
        if wants_json(request):
            return envelope_ok(_dump(view), 201)
        return crm_done(request, BACK, f"Added group {view.name}.")

    return crm_guard(request, BACK, run, db)


@router.post("/competitors/group/{group_id}", response_model=None)
def update_group(
    request: Request,
    group_id: int,
    name: str = Form(""),
    platform: str = Form(""),
    link: str | None = Form(None),
    details: str | None = Form(None),
    notes: str | None = Form(None),
    active: list[str] = Form(default_factory=list),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def run() -> Response:
        current = next((g for g in registries_service.list_groups(db) if g.id == group_id), None)
        if current is None:
            raise DomainError("not_found", "That group no longer exists - refresh the page.", 404)
        fields: dict[str, Any] = {"active": _flag(active, current.active)}
        if notes is not None:
            fields["notes"] = opt_str(notes)
        if not current.sheet_synced:  # the sheet owns the rest of a sheet-fed row
            if not name.strip():
                raise DomainError("group_invalid", "A group needs a name.", 400)
            fields["name"] = name.strip()
            if platform:
                fields["platform"] = platform.strip().lower()
            if link is not None:
                fields["link"] = opt_str(link)
            if details is not None:
                fields["details"] = opt_str(details)
        view = registries_service.update_group(db, group_id, actor=actor_of(member), **fields)
        if wants_json(request):
            return envelope_ok(_dump(view))
        return crm_done(request, BACK, f"Saved {view.name}.")

    return crm_guard(request, BACK, run, db)


@router.post("/competitors/group/{group_id}/delete", response_model=None)
def delete_group(
    request: Request,
    group_id: int,
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def run() -> Response:
        registries_service.delete_group(db, group_id, actor=actor_of(member))
        if wants_json(request):
            return envelope_ok({"deleted": group_id})
        return crm_done(request, BACK, "Group deleted.")

    return crm_guard(request, BACK, run, db)
