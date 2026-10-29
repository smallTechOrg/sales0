"""Pipeline pages (Phase 2): prospects, leads and clients with their detail pages, the
Due-today panel, notes, follow-ups and stage moves.

Routes never touch ORM objects: they call ``services.crm.*`` and render its view models.
Every mutating route answers three kinds of caller:

* htmx: updated fragments swapped out-of-band (``hx-swap="none"`` on the forms) + a toast;
* plain browser forms: ``303`` back with a flash message;
* JSON (``Accept: application/json``): the usual envelope.

A ``DomainError`` is re-raised for htmx / JSON (the envelope turns it into a toast / error)
and shown as a red flash for plain forms, exactly like ``web/routes/outreach.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc, local_today
from sales0.config import get_settings
from sales0.db.engine import get_db
from sales0.db.models import TeamMember
from sales0.errors import DomainError
from sales0.services.crm import characteristics as characteristics_service
from sales0.services.crm import clients as clients_service
from sales0.services.crm import companies as companies_service
from sales0.services.crm import contacts as contacts_service
from sales0.services.crm import flags as flags_service
from sales0.services.crm import followups as followups_service
from sales0.services.crm import leads as leads_service
from sales0.services.crm import pipeline as pipeline_service
from sales0.services.crm import prospects as prospects_service
from sales0.services.crm import timeline as timeline_service
from sales0.services.crm.stages import LEAD_STAGES, PROSPECT_STAGES, STAGE_LABELS
from sales0.web.acting import clear_flash, get_acting_member_optional, pop_flash
from sales0.web.context import PathId
from sales0.web.context import (
    actor_of,
    crm_done,
    crm_guard,
    flash_redirect,
    is_htmx,
    opt_int,
    opt_str,
    page_number,
    parse_date,
    parse_local_datetime,
    qs,
    render_html,
    render_page,
    toast_html,
)
from sales0.web.envelope import ok as envelope_ok
from sales0.web.envelope import wants_json

router = APIRouter()

TABS = ("prospects", "leads", "clients")
VIEWS = ("board", "list")
STATUSES = ("open", "closed", "converted", "all")
FLAGS = ("repeat_poster", "competitor_mention", "no_follow_up")
SOURCES = ("enquiry", "sheet", "manual", "import")
CLOSE_REASONS: tuple[tuple[str, str], ...] = (
    ("no_response", "No response"),
    ("lost", "Lost"),
    ("not_interested", "Not interested"),
)
CLIENT_STATUSES: tuple[tuple[str, str], ...] = (("active", "Active"), ("legacy", "Legacy"))
OPEN_LEAD_STAGES = ("meeting_scheduled", "proposal_sent")


def _stage_options(stages: tuple[str, ...]) -> list[tuple[str, str]]:
    return [(key, STAGE_LABELS.get(key, key)) for key in stages]


PROSPECT_STAGE_OPTIONS = _stage_options(PROSPECT_STAGES)
LEAD_STAGE_OPTIONS = _stage_options(LEAD_STAGES)


# ---------------------------------------------------------------------- shared helpers
def _today():  # noqa: ANN202
    return local_today(get_settings().tz)


def _clock_ctx() -> dict[str, Any]:
    return {"today": _today(), "one_day": timedelta(days=1)}


def _safe_next(value: str | None) -> str | None:
    """A same-site ``/pipeline...`` path (open-redirect safe) or None."""
    text = (value or "").strip()
    if not text.startswith("/pipeline") or text.startswith("//") or "\\" in text:
        return None
    return text


def _page(request: Request, db: Session, template: str, ctx: dict[str, Any]) -> HTMLResponse:
    flash = pop_flash(request)
    response = render_page(request, db, template, {**_clock_ctx(), **ctx, "flash": flash})
    if flash:
        clear_flash(response)
    return response


def _json(data: Any) -> Response:
    return envelope_ok(data.model_dump(mode="json") if hasattr(data, "model_dump") else data)


def _fragments(request: Request, parts: list[tuple[str, dict[str, Any]]], message: str, kind: str = "ok") -> HTMLResponse:
    body = "".join(render_html(request, template, {**_clock_ctx(), **ctx, "oob": True}) for template, ctx in parts)
    return HTMLResponse(body + toast_html(message, kind))


# ---------------------------------------------------------------------- list page
def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    tab = raw.get("tab") if raw.get("tab") in TABS else "prospects"
    status = raw.get("status") if raw.get("status") in STATUSES else "open"
    if tab != "prospects" and status in ("closed", "converted"):
        status = "all"
    return {
        "tab": tab,
        "view": raw.get("view") if raw.get("view") in VIEWS else "",
        "q": (raw.get("q") or "").strip()[:200],
        "stage": raw.get("stage") or "",
        "status": status,
        "flag": raw.get("flag") if raw.get("flag") in FLAGS else "",
        "source": raw.get("source") if raw.get("source") in SOURCES else "",
        "due": raw.get("due") if raw.get("due") in ("today", "overdue") else "",
        "show_removed": "1" if raw.get("show_removed") in ("1", "true", "on", True) else "",
        "characteristic": str(raw.get("characteristic") or "") if str(raw.get("characteristic") or "").isdigit() else "",
        "page": page_number(raw.get("page")),
    }


def _trait_id(params: dict[str, Any]) -> int | None:
    value = params.get("characteristic") or ""
    return int(value) if value.isdigit() and 0 < int(value) < 2**31 else None


def _trait_options(db: Session) -> list[Any]:
    return characteristics_service.list_characteristics(db)


def _pipeline_ctx(db: Session, raw: dict[str, Any]) -> dict[str, Any]:
    params = _normalise(raw)
    tab = params["tab"]
    stage_options = LEAD_STAGE_OPTIONS if tab == "leads" else PROSPECT_STAGE_OPTIONS
    valid_stages = {k for k, _ in stage_options}
    if params["stage"] not in valid_stages:
        params["stage"] = ""
    ctx: dict[str, Any] = {"tab": tab, "params": params, "stage_options": stage_options, "characteristic_options": _trait_options(db)}
    trait = _trait_id(params)

    if tab == "clients":
        page = clients_service.list_clients(db, q=params["q"] or None, characteristic_id=trait, page=params["page"])
        ctx.update(view="list", forced_list=False, items=page.items, total=page.total, page=page.page, page_size=page.page_size)
    else:
        filtered = bool(
            params["stage"] or params["status"] != "open" or (tab == "prospects" and (params["source"] or params["due"]))
        )
        view = params["view"] or "board"
        forced = filtered and view == "board"
        if filtered:
            view = "list"
        ctx.update(view=view, forced_list=forced)
        if view == "board":
            board = pipeline_service.board(
                db, kind=tab, q=params["q"] or None, flag=(params["flag"] or None) if tab == "prospects" else None,
                characteristic_id=trait,
            )
            ctx["board"] = board
        elif tab == "prospects":
            page = prospects_service.list_prospects(
                db, stage=params["stage"] or None, status=params["status"], q=params["q"] or None,
                source=params["source"] or None, flag=params["flag"] or None, due=params["due"] or None,
                characteristic_id=trait, page=params["page"],
            )  # fmt: skip
            ctx.update(items=page.items, total=page.total, page=page.page, page_size=page.page_size)
        else:
            page = leads_service.list_leads(
                db, stage=params["stage"] or None, open_only=params["status"] == "open", q=params["q"] or None,
                characteristic_id=trait, page=params["page"],
            )  # fmt: skip
            ctx.update(items=page.items, total=page.total, page=page.page, page_size=page.page_size)
    ctx["view_param"] = params["view"]
    ctx["next"] = "/pipeline?" + qs({**params, "page": params["page"] if params["page"] > 1 else None})
    return ctx


def _tab_counts(db: Session) -> list[tuple[str, str, int]]:
    summary = pipeline_service.summary(db)
    prospects = sum(summary.prospects_by_stage.values())
    leads = sum(n for stage, n in summary.leads_by_stage.items() if stage in OPEN_LEAD_STAGES)
    return [
        ("prospects", "Prospects", prospects),
        ("leads", "Leads", leads),
        ("clients", "Clients", summary.clients_active + summary.clients_legacy),
    ]


@router.get("/pipeline", response_class=HTMLResponse)
def pipeline_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    ctx = _pipeline_ctx(db, dict(request.query_params))
    if is_htmx(request):
        return HTMLResponse(render_html(request, "pipeline/_body.html", {**_clock_ctx(), **ctx}))
    ctx.update(due=followups_service.list_due(db), tabs=_tab_counts(db))
    return _page(request, db, "pipeline/page.html", ctx)


@router.get("/api/pipeline/summary")
def api_summary(db: Session = Depends(get_db)) -> Response:
    return _json(pipeline_service.summary(db))


@router.get("/api/prospects/{prospect_id}")
def api_prospect(prospect_id: PathId, db: Session = Depends(get_db)) -> Response:
    return _json(prospects_service.get_prospect(db, prospect_id))


def _board_refresh(request: Request, db: Session, next_url: str, message: str) -> HTMLResponse:
    """After a stage change from a board card: re-render the body the card came from + the Due panel."""
    raw = dict(parse_qsl(urlsplit(next_url).query))
    ctx = _pipeline_ctx(db, raw)
    parts = [("pipeline/_body.html", ctx), ("pipeline/_due.html", {"due": followups_service.list_due(db)})]
    return _fragments(request, parts, message)


# ---------------------------------------------------------------------- prospect detail


def _header_ctx(db: Session, p: Any) -> dict[str, Any]:
    company = companies_service.get_or_none(db, p.company_id) if p.company_id else None
    return {"p": p, "prospect_stage_options": PROSPECT_STAGE_OPTIONS, "close_reasons": CLOSE_REASONS,
            "characteristic_options": _trait_options(db), "company": company}


def _prospect_parts(db: Session, prospect_id: PathId) -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    p = prospects_service.get_prospect(db, prospect_id)
    base = f"/pipeline/prospects/{p.id}"
    no_fu = any(f.kind == "no_follow_up" for f in p.flags)
    parts = [
        ("pipeline/_prospect_header.html", _header_ctx(db, p)),
        ("pipeline/_timeline.html", {"timeline": p.timeline, "base": base}),
        (
            "pipeline/_followups.html",
            {"follow_ups": p.follow_ups, "base": base, "can_act": p.status == "open" and p.lead is None, "no_follow_up": no_fu},
        ),
    ]
    return p, parts


@router.get("/pipeline/prospects/{prospect_id}", response_class=HTMLResponse)
def prospect_page(request: Request, prospect_id: PathId, db: Session = Depends(get_db)) -> HTMLResponse:
    p = prospects_service.get_prospect(db, prospect_id)
    details = [contacts_service.get_contact(db, c.id) for c in p.contacts[:20]]
    return _page(request, db, "pipeline/prospect.html", {**_header_ctx(db, p), "contact_details": details})


def _prospect_action(
    request: Request, db: Session, prospect_id: PathId, action: Callable[[], str], *, next_url: str | None = None
) -> Response:
    """Run a prospect mutation; ``action`` returns the success message."""
    back = next_url or f"/pipeline/prospects/{prospect_id}"

    def run() -> Response:
        message = action()
        if wants_json(request):
            return _json(prospects_service.get_prospect(db, prospect_id))
        if is_htmx(request):
            if next_url:
                return _board_refresh(request, db, next_url, message)
            return _fragments(request, _prospect_parts(db, prospect_id)[1], message)
        return flash_redirect(back, message)

    return crm_guard(request, back, run, db)


@router.post("/pipeline/prospects", response_model=None)
def create_prospect(
    request: Request,
    name: str = Form(""),
    company: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    linkedin_url: str = Form(""),
    website: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def run() -> Response:
        if not name.strip():
            raise DomainError("prospect_invalid", "A prospect needs a name.", 400)
        view = prospects_service.create_manual(
            db, name=name.strip(), company=opt_str(company), email=opt_str(email), phone=opt_str(phone),
            linkedin_url=opt_str(linkedin_url), website=opt_str(website), notes=opt_str(notes), actor=actor_of(member),
        )  # fmt: skip
        if wants_json(request):
            return envelope_ok(view.model_dump(mode="json"), 201)
        return crm_done(request, f"/pipeline/prospects/{view.id}", f"Added {view.name}.")

    return crm_guard(request, "/pipeline?tab=prospects", run, db)


@router.post("/pipeline/prospects/{prospect_id}/stage", response_model=None)
def prospect_stage(
    request: Request,
    prospect_id: PathId,
    stage: str = Form(""),
    note: str = Form(""),
    next: str = Form(""),  # noqa: A002 - form field name
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def act() -> str:
        view = prospects_service.move_stage(db, prospect_id, stage, actor=actor_of(member), note=opt_str(note))
        return f"Moved to {view.stage_label}."

    return _prospect_action(request, db, prospect_id, act, next_url=_safe_next(next))


@router.post("/pipeline/prospects/{prospect_id}/response", response_model=None)
def prospect_response(
    request: Request,
    prospect_id: PathId,
    note: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def act() -> str:
        prospects_service.record_response(db, prospect_id, actor=actor_of(member), note=opt_str(note))
        return "Response recorded."

    return _prospect_action(request, db, prospect_id, act)


@router.post("/pipeline/prospects/{prospect_id}/convert", response_model=None)
def prospect_convert(
    request: Request,
    prospect_id: PathId,
    stage: str = Form("meeting_scheduled"),
    meeting_at: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    back = f"/pipeline/prospects/{prospect_id}"

    def run() -> Response:
        lead = leads_service.convert(
            db, prospect_id, stage=stage or "meeting_scheduled", meeting_at=parse_local_datetime(meeting_at),
            opened_by="manual", actor=actor_of(member),
        )  # fmt: skip
        if wants_json(request):
            return envelope_ok(lead.model_dump(mode="json"), 201)
        return crm_done(request, f"/pipeline/leads/{lead.id}", "Converted to a lead.")

    return crm_guard(request, back, run, db)


@router.post("/pipeline/prospects/{prospect_id}/close", response_model=None)
def prospect_close(
    request: Request,
    prospect_id: PathId,
    reason: str = Form("no_response"),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def act() -> str:
        prospects_service.close(db, prospect_id, reason.strip() or "no_response", actor=actor_of(member))
        return "Prospect closed."

    return _prospect_action(request, db, prospect_id, act)


@router.post("/pipeline/prospects/{prospect_id}/reopen", response_model=None)
def prospect_reopen(
    request: Request,
    prospect_id: PathId,
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def act() -> str:
        prospects_service.reopen(db, prospect_id, actor=actor_of(member))
        return "Prospect reopened."

    return _prospect_action(request, db, prospect_id, act)


@router.post("/pipeline/prospects/{prospect_id}/tracked", response_model=None)
def prospect_tracked(
    request: Request,
    prospect_id: PathId,
    tracked: str = Form("true"),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    on = tracked.strip().lower() in ("true", "1", "on", "yes")

    def act() -> str:
        prospects_service.set_tracked(db, prospect_id, on, actor=actor_of(member))
        return "Now tracking this person." if on else "Stopped tracking this person."

    return _prospect_action(request, db, prospect_id, act)


@router.post("/pipeline/prospects/{prospect_id}/flag", response_model=None)
def prospect_flag(
    request: Request,
    prospect_id: PathId,
    kind: str = Form("no_follow_up"),
    on: str = Form("true"),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    if kind not in FLAGS:
        raise DomainError("bad_request", "That flag is not valid.", 400)
    turn_on = on.strip().lower() in ("true", "1", "on", "yes")

    def act() -> str:
        actor = actor_of(member)
        if turn_on:
            flags_service.set_flag(db, "prospect", prospect_id, kind, source="user", actor=actor)
        else:
            flags_service.clear_flag(db, "prospect", prospect_id, kind, actor=actor)
        if kind == "no_follow_up":
            return "Marked Do not follow up; reminders cancelled." if turn_on else "Follow-ups allowed again."
        return "Flag set." if turn_on else "Flag cleared."

    return _prospect_action(request, db, prospect_id, act)


@router.post("/pipeline/prospects/{prospect_id}/edit", response_model=None)
def prospect_edit(
    request: Request,
    prospect_id: PathId,
    name: str = Form(""),
    company: str = Form(""),
    notes: str = Form(""),
    website: str | None = Form(None),
    company_linkedin_url: str | None = Form(None),
    linkedin_url: str | None = Form(None),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def act() -> str:
        if not name.strip():
            raise DomainError("prospect_invalid", "A prospect needs a name.", 400)
        extra = {k: opt_str(v) for k, v in (("website", website), ("company_linkedin_url", company_linkedin_url),
                                            ("linkedin_url", linkedin_url)) if v is not None}
        prospects_service.update(
            db, prospect_id, name=name.strip(), notes=opt_str(notes), company=opt_str(company), actor=actor_of(member), **extra
        )
        return "Details saved."

    return _prospect_action(request, db, prospect_id, act)


def _trait_ids(raw: list[str]) -> list[int]:
    out: list[int] = []
    for value in raw:
        number = opt_int(value, "characteristic")
        if number is not None:
            out.append(number)
    return out


def _set_traits(db: Session, entity_type: str, entity_id: int, raw: list[str], member: TeamMember | None) -> str:
    current = characteristics_service.set_for(db, entity_type, entity_id, _trait_ids(raw), actor=actor_of(member))
    return "Characteristics saved: " + (", ".join(c.name for c in current) if current else "none") + "."


@router.post("/pipeline/prospects/{prospect_id}/characteristics", response_model=None)
def prospect_characteristics(
    request: Request,
    prospect_id: PathId,
    characteristic_ids: list[str] = Form(default_factory=list),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    return _prospect_action(request, db, prospect_id, lambda: _set_traits(db, "prospect", prospect_id, characteristic_ids, member))


@router.post("/pipeline/prospects/{prospect_id}/contacts", response_model=None)
def prospect_add_contact(
    request: Request,
    prospect_id: PathId,
    full_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    title: str = Form(""),
    linkedin_url: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    back = f"/pipeline/prospects/{prospect_id}"

    def run() -> Response:
        view = prospects_service.add_contact(
            db, prospect_id, full_name=opt_str(full_name), email=opt_str(email), phone=opt_str(phone), title=opt_str(title),
            linkedin_url=opt_str(linkedin_url), actor=actor_of(member),
        )  # fmt: skip
        return crm_done(request, back, f"Added {view.full_name or view.email or 'the person'}.", view.model_dump(mode="json"))

    return crm_guard(request, back, run, db)


# ---------------------------------------------------------------------- notes (prospect / lead / client)
def _note(db: Session, subject_type: str, subject_id: int, body: str, member: TeamMember | None) -> None:
    text = body.strip()
    if not text:
        raise DomainError("note_invalid", "Write something before adding the note.", 400)
    timeline_service.add_note(db, subject_type=subject_type, subject_id=subject_id, body=text, actor=actor_of(member))


@router.post("/pipeline/prospects/{prospect_id}/note", response_model=None)
def prospect_note(
    request: Request,
    prospect_id: PathId,
    body: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def act() -> str:
        _note(db, "prospect", prospect_id, body, member)
        return "Note added."

    return _prospect_action(request, db, prospect_id, act)


@router.post("/pipeline/prospects/{prospect_id}/followup", response_model=None)
def prospect_followup(
    request: Request,
    prospect_id: PathId,
    due_on: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def act() -> str:
        due = parse_date(due_on, required=True, label="follow-up date")
        followups_service.add_custom(db, prospect_id=prospect_id, due_on=due, note=opt_str(note), actor=actor_of(member))  # type: ignore[arg-type]
        return "Follow-up added."

    return _prospect_action(request, db, prospect_id, act)


# ---------------------------------------------------------------------- follow-up done / snooze
def _followup_response(request: Request, db: Session, fu: Any, message: str, next_url: str | None) -> Response:
    if wants_json(request):
        return _json(fu)
    if fu.prospect_id:
        back = next_url or f"/pipeline/prospects/{fu.prospect_id}"
    else:
        back = next_url or f"/pipeline/leads/{fu.lead_id}"
    if not is_htmx(request):
        return flash_redirect(back, message)
    parts: list[tuple[str, dict[str, Any]]] = [("pipeline/_due.html", {"due": followups_service.list_due(db)})]
    if fu.prospect_id:
        parts += _prospect_parts(db, fu.prospect_id)[1]
    elif fu.lead_id:
        parts += _lead_parts(db, fu.lead_id)[1]
    return _fragments(request, parts, message)


@router.post("/pipeline/followups/{follow_up_id}/done", response_model=None)
def followup_done(
    request: Request,
    follow_up_id: PathId,
    note: str = Form(""),
    next: str = Form(""),  # noqa: A002
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    next_url = _safe_next(next)

    def run() -> Response:
        fu = followups_service.complete(db, follow_up_id, actor=actor_of(member), note=opt_str(note))
        return _followup_response(request, db, fu, "Follow-up done.", next_url)

    return crm_guard(request, next_url or "/pipeline", run, db)


@router.post("/pipeline/followups/{follow_up_id}/snooze", response_model=None)
def followup_snooze(
    request: Request,
    follow_up_id: PathId,
    due_on: str = Form(""),
    next: str = Form(""),  # noqa: A002
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    next_url = _safe_next(next)

    def run() -> Response:
        due = parse_date(due_on, required=True, label="snooze date")
        fu = followups_service.snooze(db, follow_up_id, due, actor=actor_of(member))  # type: ignore[arg-type]
        return _followup_response(request, db, fu, f"Snoozed until {due:%d %b}.", next_url)  # type: ignore[union-attr]

    return crm_guard(request, next_url or "/pipeline", run, db)


# ---------------------------------------------------------------------- lead detail
def _lead_parts(db: Session, lead_id: PathId) -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    lead = leads_service.get_lead(db, lead_id)
    base = f"/pipeline/leads/{lead.id}"
    meeting_local = ""
    if lead.meeting_at is not None:
        meeting_local = ensure_utc(lead.meeting_at).astimezone(get_settings().tz).strftime("%Y-%m-%dT%H:%M")
    parts = [
        ("pipeline/_lead_header.html", {"l": lead, "lead_stage_options": LEAD_STAGE_OPTIONS, "meeting_local": meeting_local,
                                         "characteristic_options": _trait_options(db)}),
        ("pipeline/_timeline.html", {"timeline": lead.timeline, "base": base}),
        ("pipeline/_followups.html", {"follow_ups": lead.follow_ups, "base": base, "can_act": lead.closed_at is None}),
    ]
    return lead, parts


@router.get("/pipeline/leads/{lead_id}", response_class=HTMLResponse)
def lead_page(request: Request, lead_id: PathId, db: Session = Depends(get_db)) -> HTMLResponse:
    lead, parts = _lead_parts(db, lead_id)
    return _page(request, db, "pipeline/lead.html", {"l": lead, **parts[0][1]})


def _lead_action(request: Request, db: Session, lead_id: PathId, action: Callable[[], str], *, next_url: str | None = None) -> Response:
    back = next_url or f"/pipeline/leads/{lead_id}"

    def run() -> Response:
        message = action()
        if wants_json(request):
            return _json(leads_service.get_lead(db, lead_id))
        if is_htmx(request):
            if next_url:
                return _board_refresh(request, db, next_url, message)
            return _fragments(request, _lead_parts(db, lead_id)[1], message)
        return flash_redirect(back, message)

    return crm_guard(request, back, run, db)


@router.post("/pipeline/leads/{lead_id}/stage", response_model=None)
def lead_stage(
    request: Request,
    lead_id: PathId,
    stage: str = Form(""),
    proposal_sent_on: str = Form(""),
    meeting_at: str = Form(""),
    value_estimate: str = Form(""),
    outcome_note: str = Form(""),
    next: str = Form(""),  # noqa: A002
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def act() -> str:
        view = leads_service.move_stage(
            db, lead_id, stage, proposal_sent_on=parse_date(proposal_sent_on, label="proposal date"),
            meeting_at=parse_local_datetime(meeting_at), value_estimate=opt_int(value_estimate, "value"),
            outcome_note=opt_str(outcome_note), actor=actor_of(member),
        )  # fmt: skip
        return f"Lead is now {view.stage_label}."

    return _lead_action(request, db, lead_id, act, next_url=_safe_next(next))


@router.post("/pipeline/leads/{lead_id}/characteristics", response_model=None)
def lead_characteristics(
    request: Request,
    lead_id: PathId,
    characteristic_ids: list[str] = Form(default_factory=list),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    return _lead_action(request, db, lead_id, lambda: _set_traits(db, "lead", lead_id, characteristic_ids, member))


@router.post("/pipeline/leads/{lead_id}/note", response_model=None)
def lead_note(
    request: Request,
    lead_id: PathId,
    body: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def act() -> str:
        _note(db, "lead", lead_id, body, member)
        return "Note added."

    return _lead_action(request, db, lead_id, act)


@router.post("/pipeline/leads/{lead_id}/followup", response_model=None)
def lead_followup(
    request: Request,
    lead_id: PathId,
    due_on: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def act() -> str:
        due = parse_date(due_on, required=True, label="follow-up date")
        followups_service.add_custom(db, lead_id=lead_id, due_on=due, note=opt_str(note), actor=actor_of(member))  # type: ignore[arg-type]
        return "Follow-up added."

    return _lead_action(request, db, lead_id, act)


# ---------------------------------------------------------------------- clients
def _client_parts(db: Session, client_id: PathId) -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    client = clients_service.get_client(db, client_id)
    parts = [
        ("pipeline/_client_header.html", {"c": client, "client_statuses": CLIENT_STATUSES, "characteristic_options": _trait_options(db)}),
        ("pipeline/_timeline.html", {"timeline": client.timeline, "base": f"/pipeline/clients/{client.id}"}),
    ]
    return client, parts


@router.get("/pipeline/clients/{client_id}", response_class=HTMLResponse)
def client_page(request: Request, client_id: PathId, db: Session = Depends(get_db)) -> HTMLResponse:
    client, parts = _client_parts(db, client_id)
    return _page(request, db, "pipeline/client.html", parts[0][1])


def _client_action(request: Request, db: Session, client_id: PathId, action: Callable[[], str]) -> Response:
    back = f"/pipeline/clients/{client_id}"

    def run() -> Response:
        message = action()
        if wants_json(request):
            return _json(clients_service.get_client(db, client_id))
        if is_htmx(request):
            return _fragments(request, _client_parts(db, client_id)[1], message)
        return flash_redirect(back, message)

    return crm_guard(request, back, run, db)


@router.post("/pipeline/clients", response_model=None)
def create_client(
    request: Request,
    name: str = Form(""),
    company: str = Form(""),
    started_on: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def run() -> Response:
        if not name.strip():
            raise DomainError("client_invalid", "A client needs a name.", 400)
        view = clients_service.create_legacy(
            db, name=name.strip(), company=opt_str(company), started_on=parse_date(started_on, label="start date"),
            notes=opt_str(notes), actor=actor_of(member),
        )  # fmt: skip
        if wants_json(request):
            return envelope_ok(view.model_dump(mode="json"), 201)
        return crm_done(request, f"/pipeline/clients/{view.id}", f"Added client {view.name}.")

    return crm_guard(request, "/pipeline?tab=clients", run, db)


@router.post("/pipeline/clients/{client_id}", response_model=None)
def update_client(
    request: Request,
    client_id: PathId,
    name: str = Form(""),
    notes: str = Form(""),
    status: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def act() -> str:
        if not name.strip():
            raise DomainError("client_invalid", "A client needs a name.", 400)
        clients_service.update_client(
            db, client_id, name=name.strip(), notes=opt_str(notes), status=status or "active", actor=actor_of(member)
        )
        return "Client saved."

    return _client_action(request, db, client_id, act)


@router.post("/pipeline/clients/{client_id}/characteristics", response_model=None)
def client_characteristics(
    request: Request,
    client_id: PathId,
    characteristic_ids: list[str] = Form(default_factory=list),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    return _client_action(request, db, client_id, lambda: _set_traits(db, "client", client_id, characteristic_ids, member))


@router.post("/pipeline/clients/{client_id}/note", response_model=None)
def client_note(
    request: Request,
    client_id: PathId,
    body: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def act() -> str:
        _note(db, "client", client_id, body, member)
        return "Note added."

    return _client_action(request, db, client_id, act)
