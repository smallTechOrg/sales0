"""The company page and the company-level actions (stage, Mark as lead / client, off-ramps, people, enquiry status).

A company is the one record; Prospect / Lead / Client are its stages. Every old per-stage URL (``/leads``, ``/clients``,
``/prospects/{id}``, ``/leads/{id}``, ``/clients/{id}`` and the ``/pipeline/...`` ones) redirects here so bookmarks and
e-mailed links keep working. All state changes go through ``services.crm.lifecycle`` / ``services.crm.deals``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.engine import get_db
from sales0.db.models import Client, Lead, Prospect, TeamMember
from sales0.db.models.enums_crm import PERSON_ROLE_LABELS
from sales0.errors import DomainError
from sales0.services._common import not_found
from sales0.services.crm import companies as companies_service
from sales0.services.crm import contacts as contacts_service
from sales0.services.crm import deals
from sales0.services.crm import followups as followups_service
from sales0.services.crm import lifecycle
from sales0.services.crm import prospects as prospects_service
from sales0.services.crm import timeline as timeline_service
from sales0.web.acting import get_acting_member_optional, require_admin
from sales0.web.context import (
    PathId,
    actor_of,
    crm_done,
    crm_guard,
    opt_int,
    opt_str,
    parse_date,
    parse_local_datetime,
)
from sales0.web.envelope import ok as envelope_ok
from sales0.web.envelope import wants_json
from sales0.web.routes import pipeline as pl

router = APIRouter()


def _is_admin(request: Request, db: Session) -> bool:
    try:
        require_admin(request, db)
    except DomainError:
        return False
    return True


def _company_of_prospect(db: Session, prospect_id: int) -> int | None:
    return db.scalar(select(Prospect.company_id).where(Prospect.id == prospect_id))


# ---------------------------------------------------------------------- old URLs -> the company page
def _to_company(company_id: int | None) -> RedirectResponse:
    if not company_id:
        raise not_found()
    return RedirectResponse(f"/companies/{company_id}", status_code=307)


@router.get("/leads")
def old_leads_list() -> RedirectResponse:
    return RedirectResponse("/pipeline?stage=lead", status_code=307)


@router.get("/clients")
def old_clients_list() -> RedirectResponse:
    return RedirectResponse("/pipeline?stage=client", status_code=307)


@router.get("/prospects")
def old_prospects_list() -> RedirectResponse:
    return RedirectResponse("/pipeline?stage=prospect", status_code=307)


@router.get("/prospects/{prospect_id}")
@router.get("/pipeline/prospects/{prospect_id}")
def old_prospect(prospect_id: PathId, db: Session = Depends(get_db)) -> RedirectResponse:
    return _to_company(_company_of_prospect(db, prospect_id))


@router.get("/leads/{lead_id}")
@router.get("/pipeline/leads/{lead_id}")
def old_lead(lead_id: PathId, db: Session = Depends(get_db)) -> RedirectResponse:
    row = db.get(Lead, lead_id)
    return _to_company((row.company_id or _company_of_prospect(db, row.prospect_id)) if row else None)


@router.get("/clients/{client_id}")
@router.get("/pipeline/clients/{client_id}")
def old_client(client_id: PathId, db: Session = Depends(get_db)) -> RedirectResponse:
    row = db.get(Client, client_id)
    cid = row.company_id if row else None
    if cid is None and row is not None and row.prospect_id:
        cid = _company_of_prospect(db, row.prospect_id)
    return _to_company(cid)


# ---------------------------------------------------------------------- the page
def company_context(request: Request, db: Session, company_id: int, member: TeamMember | None) -> dict[str, Any]:
    from sales0.web.routes.prospect_research import research_ctx

    life = lifecycle.life_view(db, company_id)
    co = companies_service.get_company(db, company_id)
    p = prospects_service.get_prospect(db, life.primary_prospect_id) if life.primary_prospect_id else None
    roles = lifecycle.person_roles(db, company_id)
    details = [contacts_service.get_contact(db, c.id) for c in co.contacts[:20]]
    ctx: dict[str, Any] = {
        "life": life, "co": co, "p": p, "is_admin": _is_admin(request, db), "roles": roles, "contact_details": details,
        "person_role_labels": PERSON_ROLE_LABELS, "deal_options": [(k, deals.deal_label(k)) for k in deals.SETTABLE],
        "characteristic_options": pl._trait_options(db),  # noqa: SLF001
        "team_members": [m for m in _members(db)],
        "company_timeline": co.timeline,
        "lead_follow_ups": followups_service.for_lead(db, life.open_lead_id) if life.open_lead_id else [],
    }
    if p is not None:
        no_fu = any(f.kind == "no_follow_up" for f in p.flags)
        fu = pl._followups_ctx(p, f"/pipeline/prospects/{p.id}", no_fu, db, member)  # noqa: SLF001
        ctx.update({k: fu[k] for k in ("channels", "fu_plan", "needs_contact", "draft_info", "prospect_id", "draft_due_ready")})
        ctx.update(research_ctx(db, p.id, member))
    return ctx


def _members(db: Session) -> list[TeamMember]:
    from sales0.services.team import members as members_service

    return members_service.list_members(db, include_inactive=False)


@router.get("/companies/{company_id}", response_class=HTMLResponse)
def company_page(
    request: Request, company_id: PathId, db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    if wants_json(request):
        return envelope_ok(companies_service.get_company(db, company_id).model_dump(mode="json"))
    ctx = company_context(request, db, company_id, member)
    ctx["duplicates"] = companies_service.duplicate_candidates(db, company_id)
    return pl._page(request, db, "pipeline/company.html", ctx)  # noqa: SLF001


# ---------------------------------------------------------------------- actions
def _act(request: Request, db: Session, company_id: int, message: str | None, action) -> Response:  # type: ignore[no-untyped-def]
    back = f"/companies/{company_id}"

    def run() -> Response:
        text = action() or message or "Saved."
        if wants_json(request):
            return envelope_ok(lifecycle.life_view(db, company_id).model_dump(mode="json"))
        return crm_done(request, back, text)

    return crm_guard(request, back, run, db)


@router.post("/companies/{company_id}/add-to-pipeline", response_model=None)
def add_to_pipeline(request: Request, company_id: PathId, db: Session = Depends(get_db),
                    member: TeamMember | None = Depends(get_acting_member_optional)) -> Response:
    return _act(request, db, company_id, "Added to the pipeline as a prospect.",
                lambda: lifecycle.add_to_pipeline(db, company_id, actor=actor_of(member)) and None)


@router.post("/companies/{company_id}/status", response_model=None)
def set_status(
    request: Request, company_id: PathId, sub_status: str = Form(""), proposal_sent_on: str = Form(""), meeting_at: str = Form(""),
    db: Session = Depends(get_db), member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def go() -> str:
        co = lifecycle.company_row(db, company_id)
        if co.stage == "lead":
            lifecycle.set_lead_status(db, company_id, sub_status, actor=actor_of(member),
                                      proposal_sent_on=parse_date(proposal_sent_on, label="proposal date"),
                                      meeting_at=parse_local_datetime(meeting_at))
        else:
            lifecycle.set_prospect_status(db, company_id, sub_status, actor=actor_of(member))
        return f"Now {lifecycle.sub_label(sub_status)}."

    return _act(request, db, company_id, None, go)


@router.post("/companies/{company_id}/mark-lead", response_model=None)
def mark_lead(request: Request, company_id: PathId, meeting_at: str = Form(""), db: Session = Depends(get_db),
              member: TeamMember | None = Depends(get_acting_member_optional)) -> Response:
    def go() -> str:
        lifecycle.mark_lead(db, company_id, meeting_at=parse_local_datetime(meeting_at), actor=actor_of(member))
        return "Marked as a lead."

    return _act(request, db, company_id, None, go)


@router.post("/companies/{company_id}/mark-client", response_model=None)
def mark_client(request: Request, company_id: PathId, db: Session = Depends(get_db),
                member: TeamMember | None = Depends(get_acting_member_optional)) -> Response:
    def go() -> str:
        lifecycle.mark_client(db, company_id, actor=actor_of(member))
        return "Marked as a client."

    return _act(request, db, company_id, None, go)


@router.post("/companies/{company_id}/suggestion", response_model=None)
def suggestion(request: Request, company_id: PathId, decision: str = Form(""), db: Session = Depends(get_db),
               member: TeamMember | None = Depends(get_acting_member_optional)) -> Response:
    def go() -> str:
        if decision == "accept":
            lifecycle.accept_suggestion(db, company_id, actor=actor_of(member))
            return "Marked as a lead."
        lifecycle.dismiss_suggestion(db, company_id, actor=actor_of(member))
        return "Suggestion dismissed."

    return _act(request, db, company_id, None, go)


@router.post("/companies/{company_id}/off-ramp", response_model=None)
def off_ramp(request: Request, company_id: PathId, off_ramp: str = Form(""), reason: str = Form(""),
             db: Session = Depends(get_db), member: TeamMember | None = Depends(get_acting_member_optional)) -> Response:
    def go() -> str:
        lifecycle.go_off_ramp(db, company_id, off_ramp, reason=opt_str(reason), actor=actor_of(member))
        return f"Company closed: {lifecycle.OFF_RAMP_LABELS.get(off_ramp, off_ramp)}."

    return _act(request, db, company_id, None, go)


@router.post("/companies/{company_id}/reopen", response_model=None)
def reopen(request: Request, company_id: PathId, db: Session = Depends(get_db),
           member: TeamMember | None = Depends(get_acting_member_optional)) -> Response:
    def go() -> str:
        lifecycle.reopen(db, company_id, actor=actor_of(member), is_admin=_is_admin(request, db))
        return "Company reopened."

    return _act(request, db, company_id, None, go)


@router.post("/companies/{company_id}/owner", response_model=None)
def owner(request: Request, company_id: PathId, owner_member_id: str = Form(""), db: Session = Depends(get_db),
          member: TeamMember | None = Depends(get_acting_member_optional)) -> Response:
    def go() -> str:
        lifecycle.set_owner(db, company_id, opt_int(owner_member_id, "owner"))
        return "Owner saved."

    return _act(request, db, company_id, None, go)


@router.post("/companies/{company_id}/note", response_model=None)
def note(request: Request, company_id: PathId, body: str = Form(""), db: Session = Depends(get_db),
         member: TeamMember | None = Depends(get_acting_member_optional)) -> Response:
    def go() -> str:
        text = body.strip()
        if not text:
            raise DomainError("note_invalid", "Write something before adding the note.", 400)
        lifecycle.company_row(db, company_id)
        timeline_service.add_note(db, subject_type="company", subject_id=company_id, body=text, actor=actor_of(member))
        return "Note added."

    return _act(request, db, company_id, None, go)


@router.post("/companies/{company_id}/people", response_model=None)
def add_person(
    request: Request, company_id: PathId, full_name: str = Form(""), email: str = Form(""), phone: str = Form(""),
    title: str = Form(""), linkedin_url: str = Form(""), role: str = Form(""), role_other: str = Form(""),
    db: Session = Depends(get_db), member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def go() -> str:
        chosen = (role_other.strip() if role == "other" and role_other.strip() else PERSON_ROLE_LABELS.get(role, role)) or None
        view = lifecycle.add_person(db, company_id, full_name=opt_str(full_name), email=opt_str(email), phone=opt_str(phone),
                                    title=opt_str(title), linkedin_url=opt_str(linkedin_url), role=chosen, actor=actor_of(member))
        return f"Added {view.full_name or view.email or 'the person'}."

    return _act(request, db, company_id, None, go)


@router.post("/companies/{company_id}/people/{contact_id}/role", response_model=None)
def person_role(request: Request, company_id: PathId, contact_id: PathId, role: str = Form(""), role_other: str = Form(""),
                db: Session = Depends(get_db), member: TeamMember | None = Depends(get_acting_member_optional)) -> Response:
    def go() -> str:
        chosen = (role_other.strip() if role == "other" and role_other.strip() else PERSON_ROLE_LABELS.get(role, role)) or None
        lifecycle.set_person_role(db, company_id, contact_id, chosen)
        return "Role saved."

    return _act(request, db, company_id, None, go)


@router.post("/pipeline/enquiries/{enquiry_id}/status", response_model=None)
def enquiry_status(
    request: Request, enquiry_id: PathId, status: str = Form(""), next: str = Form(""),  # noqa: A002
    db: Session = Depends(get_db), member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    from sales0.db.models import Enquiry

    e = db.get(Enquiry, enquiry_id)
    cid = e.company_id if e else None
    back = pl._safe_next(next) or (f"/companies/{cid}" if cid else "/pipeline?view=enquiries")  # noqa: SLF001

    def run() -> Response:
        row = deals.set_status(db, enquiry_id, status, actor=actor_of(member))
        msg = f"Enquiry status: {deals.deal_label(row.deal_status)}."
        if wants_json(request):
            return envelope_ok({"id": row.id, "deal_status": row.deal_status})
        return crm_done(request, back, msg)

    return crm_guard(request, back, run, db)
