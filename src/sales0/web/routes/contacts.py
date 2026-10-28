"""Contacts pages (Phase 2): searchable address book, detail with tags and timeline, duplicate
merge flow, and the read-only company page.

Same conventions as ``pipeline.py``: htmx -> fragment/redirect + toast, plain forms -> 303 + flash,
JSON -> envelope; a ``DomainError`` is a red flash for browser forms.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from sales0.db.engine import get_db
from sales0.db.models import TeamMember
from sales0.errors import DomainError
from sales0.services.crm import companies as companies_service
from sales0.services.crm import contacts as contacts_service
from sales0.web.acting import clear_flash, get_acting_member_optional, pop_flash
from sales0.web.context import PathId
from sales0.web.context import (
    actor_of,
    crm_done,
    crm_guard,
    is_htmx,
    opt_int,
    opt_str,
    page_number,
    render_html,
    render_page,
)
from sales0.web.envelope import ok as envelope_ok
from sales0.web.envelope import wants_json

router = APIRouter()

TAG_CATEGORIES = ("custom", "role", "industry", "source", "relationship")
DUPLICATE_SCAN_LIMIT = 50


def _page(request: Request, db: Session, template: str, ctx: dict[str, Any]) -> HTMLResponse:
    flash = pop_flash(request)
    response = render_page(request, db, template, {**ctx, "flash": flash})
    if flash:
        clear_flash(response)
    return response


def _params(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "q": (raw.get("q") or "").strip()[:200],
        "tag": (raw.get("tag") or "").strip()[:40],
        "company_id": opt_int(raw.get("company_id") or "") if str(raw.get("company_id") or "").isdigit() else None,
        "page": page_number(raw.get("page")),
    }


def _dump(data: Any) -> Any:
    return data.model_dump(mode="json") if hasattr(data, "model_dump") else data


@router.get("/contacts", response_class=HTMLResponse)
def contacts_page(request: Request, db: Session = Depends(get_db)) -> Response:
    params = _params(dict(request.query_params))
    page = contacts_service.list_contacts(
        db, q=params["q"] or None, tag=params["tag"] or None, company_id=params["company_id"], page=params["page"]
    )
    if wants_json(request):
        return envelope_ok(_dump(page))
    if is_htmx(request):
        return HTMLResponse(render_html(request, "contacts/_results.html", {"page": page, "params": params}))
    company = companies_service.get_company(db, params["company_id"]) if params["company_id"] else None
    return _page(
        request, db, "contacts/list.html",
        {
            "page": page, "params": params, "tags": contacts_service.list_tags(db), "company": company,
            "dup_count": len(contacts_service.find_duplicates(db, limit=DUPLICATE_SCAN_LIMIT)),
        },
    )  # fmt: skip


@router.get("/contacts/duplicates", response_class=HTMLResponse)
def duplicates_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    groups = contacts_service.find_duplicates(db, limit=DUPLICATE_SCAN_LIMIT)
    return _page(request, db, "contacts/duplicates.html", {"groups": groups})


@router.get("/contacts/{contact_id}", response_class=HTMLResponse)
def contact_page(request: Request, contact_id: PathId, db: Session = Depends(get_db)) -> Response:
    contact = contacts_service.get_contact(db, contact_id)
    if contact.id != contact_id and not wants_json(request):  # merged away: send the old link to the survivor
        return RedirectResponse(f"/contacts/{contact.id}", status_code=303)
    if wants_json(request):
        return envelope_ok(_dump(contact))
    return _page(
        request, db, "contacts/detail.html",
        {"c": contact, "all_tags": contacts_service.list_tags(db), "tag_categories": TAG_CATEGORIES},
    )  # fmt: skip


@router.get("/companies/{company_id}", response_class=HTMLResponse)
def company_page(request: Request, company_id: PathId, db: Session = Depends(get_db)) -> HTMLResponse:
    return _page(request, db, "contacts/company.html", {"co": companies_service.get_company(db, company_id)})


# ---------------------------------------------------------------------- mutations
@router.post("/contacts", response_model=None)
def create_contact(
    request: Request,
    full_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    linkedin_url: str = Form(""),
    title: str = Form(""),
    company: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def run() -> Response:
        company_id = None
        if opt_str(company):
            company_id = companies_service.get_or_create(db, company.strip()).id
        view = contacts_service.upsert(
            db, full_name=opt_str(full_name), email=opt_str(email), phone=opt_str(phone), linkedin_url=opt_str(linkedin_url),
            title=opt_str(title), company_id=company_id, origin="manual", actor=actor_of(member),
        )  # fmt: skip
        if wants_json(request):
            return envelope_ok(_dump(view), 201)
        return crm_done(request, f"/contacts/{view.id}", f"Saved {view.full_name or view.email}.")

    return crm_guard(request, "/contacts", run, db)


@router.post("/contacts/merge", response_model=None)
def merge_contacts(
    request: Request,
    keep_id: str = Form(""),
    drop_id: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def run() -> Response:
        keep, drop = opt_int(keep_id, "contact"), opt_int(drop_id, "contact")
        if keep is None or drop is None:
            raise DomainError("bad_request", "Choose two contacts to merge.", 400)
        view = contacts_service.merge(db, keep, drop, actor=actor_of(member))
        if wants_json(request):
            return envelope_ok(_dump(view))
        return crm_done(request, "/contacts/duplicates", f"Merged into {view.full_name or view.email}.")

    return crm_guard(request, "/contacts/duplicates", run, db)


@router.post("/contacts/{contact_id}", response_model=None)
def update_contact(
    request: Request,
    contact_id: PathId,
    full_name: str = Form(""),
    title: str = Form(""),
    notes: str = Form(""),
    company: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    back = f"/contacts/{contact_id}"

    def run() -> Response:
        view = contacts_service.update_contact(
            db, contact_id, full_name=opt_str(full_name), title=opt_str(title), notes=opt_str(notes),
            company=opt_str(company), actor=actor_of(member),
        )  # fmt: skip
        if wants_json(request):
            return envelope_ok(_dump(view))
        return crm_done(request, back, "Contact saved.")

    return crm_guard(request, back, run, db)


@router.post("/contacts/{contact_id}/tags", response_model=None)
def add_tag(
    request: Request,
    contact_id: PathId,
    name: str = Form(""),
    category: str = Form("custom"),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    back = f"/contacts/{contact_id}"

    def run() -> Response:
        view = contacts_service.add_tag(db, contact_id, name.strip(), category.strip() or "custom", actor=actor_of(member))
        if wants_json(request):
            return envelope_ok(_dump(view))
        return crm_done(request, back, f"Tag '{name.strip()}' added.")

    return crm_guard(request, back, run, db)


@router.post("/contacts/{contact_id}/tags/{tag_id}/delete", response_model=None)
def remove_tag(
    request: Request,
    contact_id: PathId,
    tag_id: PathId,
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    back = f"/contacts/{contact_id}"

    def run() -> Response:
        view = contacts_service.remove_tag(db, contact_id, tag_id, actor=actor_of(member))
        if wants_json(request):
            return envelope_ok(_dump(view))
        return crm_done(request, back, "Tag removed.")

    return crm_guard(request, back, run, db)
