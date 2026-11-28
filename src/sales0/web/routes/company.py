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
from sales0.services.crm import company_offerings, companies as companies_service
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
    flash_redirect,
    is_htmx,
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
    return RedirectResponse("/pipeline?tab=leads", status_code=307)


@router.get("/clients")
def old_clients_list() -> RedirectResponse:
    return RedirectResponse("/pipeline?tab=clients", status_code=307)


@router.get("/prospects")
def old_prospects_list() -> RedirectResponse:
    return RedirectResponse("/pipeline?tab=prospects", status_code=307)


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
        "offering_options": pl._offering_options(db),  # noqa: SLF001
        "company_offering_ids": company_offerings.ids_for(db, company_id),
    }
    if p is not None:
        no_fu = any(f.kind == "no_follow_up" for f in p.flags)
        fu = pl._followups_ctx(p, f"/pipeline/prospects/{p.id}", no_fu, db, member)  # noqa: SLF001
        ctx.update({k: fu[k] for k in ("channels", "fu_plan", "needs_contact", "draft_info", "prospect_id", "draft_due_ready", "fu_notes", "reminders", "fu_people", "fu_hows", "ai_down")})
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
    merge_q = (request.query_params.get("merge_q") or "").strip()[:100]
    ctx["duplicates"] = companies_service.merge_candidates(db, company_id, merge_q)
    ctx["merge_q"] = merge_q
    return pl._page(request, db, "pipeline/company.html", ctx)  # noqa: SLF001


@router.get("/companies/{company_id}/merge-candidates", response_class=HTMLResponse)
def company_merge_candidates(request: Request, company_id: PathId, merge_q: str = "", db: Session = Depends(get_db)) -> Response:
    """SMA-62: the options of the merge picker for what was typed (htmx swaps them into the select)."""
    from sales0.web.context import render_html

    found = companies_service.merge_candidates(db, company_id, merge_q.strip()[:100])
    return HTMLResponse(render_html(request, "pipeline/_merge_options.html", {"duplicates": found, "merge_q": merge_q}))


# ---------------------------------------------------------------------- actions
def _act(request: Request, db: Session, company_id: int, message: str | None, action) -> Response:  # type: ignore[no-untyped-def]
    back = f"/companies/{company_id}"

    def run() -> Response:
        text = action() or message or "Saved."
        if wants_json(request):
            return envelope_ok(lifecycle.life_view(db, company_id).model_dump(mode="json"))
        return crm_done(request, back, text)

    return crm_guard(request, back, run, db)


@router.post("/companies/{company_id}/offerings", response_model=None)
def set_offerings(
    request: Request, company_id: PathId, offering_ids: list[int] = Form(default_factory=list), db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    """The offerings this company is a fit for (several allowed); a person's choice replaces any AI tags."""
    def act() -> str:
        ids = company_offerings.set_for(db, company_id, offering_ids, source="manual")
        return f"Offerings saved ({len(ids)})." if ids else "Offerings cleared."

    return _act(request, db, company_id, None, act)


@router.post("/companies/{company_id}/add-to-pipeline", response_model=None)
def add_to_pipeline(request: Request, company_id: PathId, db: Session = Depends(get_db),
                    member: TeamMember | None = Depends(get_acting_member_optional)) -> Response:
    return _act(request, db, company_id, "Added to the pipeline as a prospect.",
                lambda: lifecycle.add_to_pipeline(db, company_id, actor=actor_of(member)) and None)


@router.post("/companies/{company_id}/status", response_model=None)
def set_status(
    request: Request, company_id: PathId, sub_status: str = Form(""), proposal_sent_on: str = Form(""), meeting_at: str = Form(""),
    value_estimate: str = Form(""), outcome_note: str = Form(""), db: Session = Depends(get_db), member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def go() -> str:
        co = lifecycle.company_row(db, company_id)
        if co.stage == "lead":
            lifecycle.set_lead_status(db, company_id, sub_status, actor=actor_of(member),
                                      proposal_sent_on=parse_date(proposal_sent_on, label="proposal date"),
                                      meeting_at=parse_local_datetime(meeting_at), value_estimate=opt_int(value_estimate, "value"),
                                      outcome_note=opt_str(outcome_note))
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
        return "Marked as a customer."

    return _act(request, db, company_id, None, go)


@router.post("/companies/{company_id}/move", response_model=None)
def move(
    request: Request, company_id: PathId, target: str = Form(""), why: str = Form(""), meeting_at: str = Form(""),
    next: str = Form(""), db: Session = Depends(get_db), member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    """Set the status by hand (the "..." menu, a board drag): any board column, Client or a close reason; a client marked
    by mistake goes back to any Leads / Prospects status. From a board, the board is re-rendered."""
    back = pl._safe_next(next)  # noqa: SLF001

    def go() -> str:
        lifecycle.move_to(db, company_id, target, actor=actor_of(member), why=opt_str(why),
                          meeting_at=parse_local_datetime(meeting_at), is_admin=_is_admin(request, db))
        name = lifecycle.company_row(db, company_id).name
        return f"{name}: {lifecycle.move_label(target)}."

    if back and not wants_json(request):
        def run() -> Response:
            text = go()
            if is_htmx(request):
                return pl._board_refresh(request, db, back, text)  # noqa: SLF001
            return flash_redirect(back, text)

        return crm_guard(request, back, run, db)
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
    location: str = Form(""),
    db: Session = Depends(get_db), member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def go() -> str:
        chosen = (role_other.strip() if role == "other" and role_other.strip() else PERSON_ROLE_LABELS.get(role, role)) or None
        view = lifecycle.add_person(db, company_id, full_name=opt_str(full_name), email=opt_str(email), phone=opt_str(phone),
                                    title=opt_str(title), linkedin_url=opt_str(linkedin_url), role=chosen, actor=actor_of(member),
                                    location=opt_str(location))
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
    back = _safe_enquiry_next(next) or (f"/companies/{cid}" if cid else "/pipeline?tab=enquiries")

    def run() -> Response:
        row = deals.set_status(db, enquiry_id, status, actor=actor_of(member))
        msg = f"Enquiry status: {deals.deal_label(row.deal_status)}."
        if wants_json(request):
            return envelope_ok({"id": row.id, "deal_status": row.deal_status})
        return crm_done(request, back, msg)

    return crm_guard(request, back, run, db)


def _safe_enquiry_next(value: str) -> str:
    """A same-site /companies, /pipeline or /inbox path (open-redirect safe), else ''."""
    nxt = (value or "").strip()
    return nxt if nxt.startswith(("/companies/", "/pipeline", "/inbox", "/enquiries/")) and not nxt.startswith("//") and "\\" not in nxt else ""


@router.post("/enquiries/{enquiry_id}/edit", response_model=None)
def enquiry_edit(
    request: Request, enquiry_id: PathId, requirement_summary: str = Form(""), company: str = Form(""),
    contact_person: str = Form(""), email: str = Form(""), phone: str = Form(""), city: str = Form(""),
    author_name: str = Form(""), source_url: str = Form(""), author_profile_url: str = Form(""), notes: str = Form(""),
    date_posted: str = Form(""), next: str = Form(""),  # noqa: A002
    db: Session = Depends(get_db), member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    """SMA-40: edit an enquiry's details by hand (every field of the form is saved as given; empty clears it)."""
    from sales0.db.models import Enquiry
    from sales0.services.crm import enquiry_edit

    e = db.get(Enquiry, enquiry_id)
    cid = e.company_id if e else None
    safe = _safe_enquiry_next(next)
    back = safe or (f"/companies/{cid}#enquiry-{enquiry_id}" if cid else "/inbox")

    def run() -> Response:
        fields = {"requirement_summary": requirement_summary, "company": company, "contact_person": contact_person, "email": email,
                  "phone": phone, "city": city, "author_name": author_name, "source_url": source_url,
                  "author_profile_url": author_profile_url, "notes": notes}
        row = enquiry_edit.edit(db, enquiry_id, fields=fields, date_posted=parse_date(date_posted, label="date posted"),
                                clear_date=not date_posted.strip(), actor=actor_of(member))
        if wants_json(request):
            return envelope_ok({"id": row.id, "company_id": row.company_id})
        return crm_done(request, back if not (row.company_id and back.startswith("/companies/") and cid != row.company_id)
                        else f"/companies/{row.company_id}#enquiry-{row.id}", "Enquiry saved.")

    return crm_guard(request, back, run, db)


@router.get("/enquiries/{enquiry_id}", response_class=HTMLResponse)
def enquiry_page(request: Request, enquiry_id: PathId, db: Session = Depends(get_db)) -> Response:
    """SMA-59: one enquiry on a page of its own - every detail (the post and its LinkedIn links, the author, the contact,
    the AI's reading), its status and the edit form."""
    from sales0.db.models import Company, Contact, Enquiry, Offering
    from sales0.services.crm import priority as priority_service
    from sales0.web.context import render_page

    e = db.get(Enquiry, enquiry_id)
    if e is None:
        raise not_found()
    if wants_json(request):
        return envelope_ok({"id": e.id, "company_id": e.company_id, "requirement_summary": e.requirement_summary,
                            "deal_status": e.deal_status, "review_status": e.review_status})
    company = db.get(Company, e.company_id) if e.company_id else None
    contact = db.get(Contact, e.contact_id) if e.contact_id else None
    offering = db.get(Offering, e.offering_id) if e.offering_id else None
    company_priority = db.scalar(select(priority_service.of_company(Company.id)).where(Company.id == e.company_id)) if company else None
    ctx = {"e": e, "company": company, "contact": contact, "offering": offering, "deal_options": [(k, deals.deal_label(k)) for k in deals.SETTABLE],
           "deal_label": deals.deal_label(e.deal_status) if e.deal_status else None, "company_priority": company_priority,
           "self_url": f"/enquiries/{e.id}"}
    return render_page(request, db, "pipeline/enquiry_page.html", ctx)


@router.get("/enquiries/{enquiry_id}/edit-form", response_class=HTMLResponse)
def enquiry_edit_form(request: Request, enquiry_id: PathId, next: str = "", db: Session = Depends(get_db)) -> HTMLResponse:  # noqa: A002
    """The edit form (SMA-40): a fragment for the Inbox's lazy "Edit details", a page for a plain link."""
    from sales0.db.models import Enquiry
    from sales0.web.context import render_fragment, render_page

    e = db.get(Enquiry, enquiry_id)
    if e is None:
        raise not_found()
    safe = _safe_enquiry_next(next)
    if is_htmx(request):
        return render_fragment(request, "pipeline/_enquiry_edit_fragment.html", {"e": e, "next": safe})
    return render_page(request, db, "pipeline/enquiry_edit_page.html", {"e": e, "next": safe})
