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
from sqlalchemy import select as select_
from sqlalchemy.orm import Session

from sales0.clock import local_today
from sales0.config import get_settings
from sales0.db.engine import get_db
from sales0.db.models import Prospect as Prospect_
from sales0.db.models import TeamMember
from sales0.errors import DomainError
from sales0.services.crm import characteristics as characteristics_service
from sales0.services.crm import clients as clients_service
from sales0.services.crm import companies as companies_service
from sales0.services.crm import flags as flags_service
from sales0.services.crm import followups as followups_service
from sales0.services.crm import company_pipeline as company_pipeline_service
from sales0.services.crm import leads as leads_service
from sales0.services.crm import lifecycle as lifecycle_service
from sales0.services.crm import pipeline as pipeline_service
from sales0.services.crm import priority as priority_service
from sales0.services.crm import prospects as prospects_service
from sales0.services.crm import timeline as timeline_service
from sales0.services.crm.stages import LEAD_STAGES, PROSPECT_STAGES, STAGE_LABELS
from sales0.llm import LLMAuthError, LLMClient, LLMModelNotFound, get_llm_client
from sales0.services import followup_drafts as followup_drafts_service
from sales0.web.acting import clear_flash, get_acting_member, get_acting_member_optional, pop_flash
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

VIEWS = ("board", "list")
STAGE_CHOICES: tuple[tuple[str, str], ...] = (("prospect", "Prospect"), ("lead", "Lead"), ("client", "Client"), ("closed", "Closed"))
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
CATEGORY_CHOICES: tuple[tuple[str, str], ...] = (
    ("relevant_prospect", "Relevant prospect"), ("channel_partner", "Channel partner"), ("competitor", "Competitor"),
)


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


def get_llm() -> LLMClient:
    """LLM seam for the follow-up drafts (``sales0.llm.set_llm_client`` in tests)."""
    return get_llm_client()


def _safe_back(value: str | None, default: str) -> str:
    """A same-site /pipeline or /outreach path (open-redirect safe), else ``default``."""
    text = (value or "").strip()
    if text.startswith(("/pipeline", "/outreach")) and not text.startswith("//") and "\\" not in text:
        return text
    return default


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
VIEWS_OF = ("companies", "enquiries")  # what the board / list shows
STAGE_FILTERS = ("prospect", "lead", "client", "closed")


def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    # old bookmarks: ?tab=prospects|leads|clients meant a stage
    stage = raw.get("stage") if raw.get("stage") in STAGE_FILTERS else ""
    old = {"prospects": "prospect", "leads": "lead", "clients": "client"}.get(str(raw.get("tab") or ""))
    if not stage and old:
        stage = old
    return {
        "view": raw.get("view") if raw.get("view") in VIEWS_OF else "companies",
        "layout": raw.get("layout") if raw.get("layout") in VIEWS else ("list" if raw.get("view") == "list" else ""),
        "q": (raw.get("q") or "").strip()[:200],
        "stage": stage,
        "flag": raw.get("flag") if raw.get("flag") in FLAGS else "",
        "characteristic": str(raw.get("characteristic") or "") if str(raw.get("characteristic") or "").isdigit() else "",
        "priority": raw.get("priority") if raw.get("priority") in ("high", "medium", "low", "none") else "",
        "category": raw.get("category") if raw.get("category") in ("relevant_prospect", "channel_partner", "competitor") else "",
        "sort": "priority" if raw.get("sort") == "priority" else "",
        "page": page_number(raw.get("page")),
    }


def _trait_id(params: dict[str, Any]) -> int | None:
    value = params.get("characteristic") or ""
    return int(value) if value.isdigit() and 0 < int(value) < 2**31 else None


def _trait_options(db: Session) -> list[Any]:
    return characteristics_service.list_characteristics(db)


def _pipeline_ctx(db: Session, raw: dict[str, Any]) -> dict[str, Any]:
    params = _normalise(raw)
    trait = _trait_id(params)
    ctx: dict[str, Any] = {"params": params, "characteristic_options": _trait_options(db), "priority_choices": priority_service.FILTER_CHOICES,
                           "category_choices": CATEGORY_CHOICES, "stage_choices": STAGE_CHOICES}
    if params["view"] == "enquiries":
        ctx.update(layout="board", forced_list=False, enquiry_board=company_pipeline_service.enquiry_board(
            db, q=params["q"] or None, characteristic_id=trait, priority_filter=params["priority"] or None,
            category=params["category"] or None))
    else:
        layout = params["layout"] or "board"
        # one stage chosen -> the list (the board always shows the three stages side by side)
        forced = bool(params["stage"]) and layout == "board"
        if params["stage"]:
            layout = "list"
        ctx.update(layout=layout, forced_list=forced)
        common = dict(q=params["q"] or None, characteristic_id=trait, priority_filter=params["priority"] or None,
                      category=params["category"] or None, flag=params["flag"] or None, sort=params["sort"] or None)
        if layout == "board":
            ctx["company_board"] = company_pipeline_service.board(db, **common)
        else:
            page = company_pipeline_service.company_list(
                db, stage=params["stage"] if params["stage"] in ("prospect", "lead", "client") else None,
                closed=True if params["stage"] == "closed" else False, page=params["page"], **common)
            ctx.update(items=page.items, total=page.total, page=page.page, page_size=page.page_size)
    ctx["next"] = "/pipeline?" + qs({**params, "page": params["page"] if params["page"] > 1 else None})
    return ctx


def _tab_counts(db: Session) -> list[tuple[str, str, int]]:
    c = company_pipeline_service.counts(db)
    return [("prospect", "Prospects", c["prospect"]), ("lead", "Leads", c["lead"]), ("client", "Clients", c["client"]),
            ("closed", "Closed", c["closed"])]


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


def _header_ctx(db: Session, p: Any, member: TeamMember | None = None) -> dict[str, Any]:
    from sales0.services.team import members as members_service

    return {"p": p, "life": lifecycle_service.life_view(db, p.company_id) if p.company_id else None,
            "co": companies_service.get_company(db, p.company_id) if p.company_id else None,
            "characteristic_options": _trait_options(db), "is_admin": bool(member is not None and member.role == "admin"),
            "team_members": members_service.list_members(db, include_inactive=False)}


def _prospect_parts(db: Session, prospect_id: PathId, member: TeamMember | None = None) -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    p = prospects_service.get_prospect(db, prospect_id)
    base = f"/pipeline/prospects/{p.id}"
    no_fu = any(f.kind == "no_follow_up" for f in p.flags)
    tl = timeline_service.list_timeline(db, company_id=p.company_id) if p.company_id else p.timeline
    parts = [
        ("pipeline/_prospect_header.html", _header_ctx(db, p, member)),
        ("pipeline/_timeline.html", {"timeline": tl, "base": base}),
        ("pipeline/_followups.html", _followups_ctx(p, base, no_fu, db, member)),
    ]
    return p, parts


def _followups_ctx(p: Any, base: str, no_fu: bool, db: Session | None = None, member: TeamMember | None = None) -> dict[str, Any]:
    """The follow-ups card: open + done reminders, the channel choices and, before the first contact, the planned
    Follow-up 1 / 2 / Final dates (editable in the Log contact form; they become live reminders when it is saved)."""
    can_act = p.status == "open" and p.lead is None
    needs_contact = can_act and p.stage == "new" and (p.contacted_at is None or p.contacted_estimated)
    info = _draft_info(db, p, member) if db is not None and can_act else {}
    today = _today()
    return {
        "follow_ups": p.follow_ups, "base": base, "can_act": can_act, "no_follow_up": no_fu,
        "channels": list(followups_service.CHANNEL_LABELS.items()),
        "fu_plan": followups_service.plan(_today()) if needs_contact else [],
        "needs_contact": needs_contact,
        "draft_info": info, "prospect_id": p.id,
        "draft_due_ready": sum(1 for v in p.follow_ups if not v.done and v.due_on <= today and info.get(v.id) is not None and info[v.id].ok),
    }  # fmt: skip


def _draft_info(db: Session, p: Any, member: TeamMember | None) -> dict[int, Any]:
    """Per open email follow-up of the prospect: can an AI email draft be written (or why not)."""
    from sales0.db.models import FollowUp

    rows = [f for f in (db.get(FollowUp, v.id) for v in p.follow_ups if not v.done) if f is not None]
    return followup_drafts_service.explain(db, rows, member)


def _company_url(db: Session, prospect_id: int) -> str:
    from sales0.db.models import Prospect

    cid = db.scalar(select_(Prospect.company_id).where(Prospect.id == prospect_id))
    return f"/companies/{cid}" if cid else "/pipeline"


def _prospect_action(
    request: Request, db: Session, prospect_id: PathId, action: Callable[[], str], *, next_url: str | None = None,
    member: TeamMember | None = None,
) -> Response:
    """Run a prospect mutation; ``action`` returns the success message."""
    back = next_url or _company_url(db, prospect_id)

    def run() -> Response:
        message = action()
        if wants_json(request):
            return _json(prospects_service.get_prospect(db, prospect_id))
        if is_htmx(request):
            if next_url:
                return _board_refresh(request, db, next_url, message)
            return _fragments(request, _prospect_parts(db, prospect_id, member or get_acting_member_optional(request, db))[1], message)
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
        who = name.strip() or company.strip()  # the form asks for the company; a person is optional
        if not who:
            raise DomainError("prospect_invalid", "A company needs a name.", 400)
        view = prospects_service.create_manual(
            db, name=who, company=opt_str(company), email=opt_str(email), phone=opt_str(phone),
            linkedin_url=opt_str(linkedin_url), website=opt_str(website), notes=opt_str(notes), actor=actor_of(member),
        )  # fmt: skip
        if wants_json(request):
            return envelope_ok(view.model_dump(mode="json"), 201)
        return crm_done(request, f"/companies/{view.company_id}" if view.company_id else "/pipeline", f"Added {view.company_name or view.name}.")

    return crm_guard(request, "/pipeline", run, db)


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


@router.post("/pipeline/prospects/{prospect_id}/priority", response_model=None)
def prospect_priority(
    request: Request,
    prospect_id: PathId,
    priority: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    def act() -> str:
        p = priority_service.set_priority(db, prospect_id, priority, actor=actor_of(member))
        if p.priority_source == "manual":
            return f"Priority set to {priority_service.LABELS[p.priority or 'low']}."
        return "Priority is automatic again" + (f" ({priority_service.LABELS[p.priority]})." if p.priority else ".")

    return _prospect_action(request, db, prospect_id, act)


@router.post("/pipeline/prospects/{prospect_id}/enquiries/{enquiry_id}/category", response_model=None)
def prospect_enquiry_category(
    request: Request,
    prospect_id: PathId,
    enquiry_id: PathId,
    category: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    """E3: change an enquiry's category from the prospect page. Channel partner ticks Partner on the prospect; Competitor
    only labels it here (the Inbox / Competitors registry files it)."""
    from sales0.db.models import Enquiry
    from sales0.services import categories

    def act() -> str:
        e = db.get(Enquiry, enquiry_id)
        if e is None or e.prospect_id != prospect_id:
            raise DomainError("not_found", "That enquiry is not linked to this prospect.", 404)
        categories.set_category(db, enquiry_id, category, actor_user_id=member.id if member else None)
        return f"Category set to {categories.label(category.strip().lower())}."

    return _prospect_action(request, db, prospect_id, act)


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
    back = _company_url(db, prospect_id)

    def run() -> Response:
        cid = db.scalar(select_(Prospect_.company_id).where(Prospect_.id == prospect_id))
        if cid is None:
            raise DomainError("not_found", "That item no longer exists - refresh the page.", 404)
        lifecycle_service.mark_lead(db, cid, stage=stage or "meeting_scheduled", meeting_at=parse_local_datetime(meeting_at),
                                    actor=actor_of(member))
        if wants_json(request):
            return envelope_ok(lifecycle_service.life_view(db, cid).model_dump(mode="json"), 201)
        return crm_done(request, back, "Marked as a lead.")

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


@router.post("/pipeline/prospects/{prospect_id}/contact", response_model=None)
def prospect_log_contact(
    request: Request,
    prospect_id: PathId,
    channel: str = Form(""),
    note: str = Form(""),
    contacted_on: str = Form(""),
    due_follow_up_1: str = Form(""),
    due_follow_up_2: str = Form(""),
    due_final_follow_up: str = Form(""),
    channel_follow_up_1: str = Form(""),
    channel_follow_up_2: str = Form(""),
    channel_final_follow_up: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    """Log the FIRST contact by hand (WhatsApp / LinkedIn / Email / Call): New -> Contacted + the three follow-ups."""

    def act() -> str:
        dues = {
            "follow_up_1": parse_date(due_follow_up_1, label="Follow-up 1 date"),
            "follow_up_2": parse_date(due_follow_up_2, label="Follow-up 2 date"),
            "final_follow_up": parse_date(due_final_follow_up, label="Final follow-up date"),
        }
        view = prospects_service.log_contact(
            db, prospect_id, channel=channel, note=opt_str(note), contacted_on=parse_date(contacted_on, label="contact date"),  # type: ignore[arg-type]
            due_dates=dues, actor=actor_of(member),
            channels={"follow_up_1": opt_str(channel_follow_up_1), "follow_up_2": opt_str(channel_follow_up_2),
                      "final_follow_up": opt_str(channel_final_follow_up)},
        )  # fmt: skip
        how = followups_service.CHANNEL_LABELS.get(channel.strip().lower(), channel)
        return f"Contact via {how} logged - {view.stage_label}; the three follow-ups are scheduled."

    return _prospect_action(request, db, prospect_id, act)


# ---------------------------------------------------------------------- follow-up done / snooze
def _followup_response(request: Request, db: Session, fu: Any, message: str, next_url: str | None) -> Response:
    if wants_json(request):
        return _json(fu)
    if fu.prospect_id:
        back = next_url or _company_url(db, fu.prospect_id)
    else:
        from sales0.db.models import Lead

        row = db.get(Lead, fu.lead_id)
        cid = (row.company_id or db.scalar(select_(Prospect_.company_id).where(Prospect_.id == row.prospect_id))) if row else None
        back = next_url or (f"/companies/{cid}" if cid else "/pipeline")
    if not is_htmx(request):
        return flash_redirect(back, message)
    parts: list[tuple[str, dict[str, Any]]] = [("pipeline/_due.html", {"due": followups_service.list_due(db)})]
    if fu.prospect_id:
        parts += _prospect_parts(db, fu.prospect_id, get_acting_member_optional(request, db))[1]
        return _fragments(request, parts, message)
    return crm_done(request, back, message)


@router.post("/pipeline/followups/{follow_up_id}/done", response_model=None)
def followup_done(
    request: Request,
    follow_up_id: PathId,
    note: str = Form(""),
    channel: str = Form(""),
    next: str = Form(""),  # noqa: A002
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    next_url = _safe_next(next)

    def run() -> Response:
        fu = followups_service.complete(db, follow_up_id, actor=actor_of(member), note=opt_str(note), channel=opt_str(channel))
        how = f" via {fu.channel_label}" if fu.channel_label else ""
        return _followup_response(request, db, fu, f"Follow-up done{how}.", next_url)

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


# ---------------------------------------------------------------------- planned channel + AI-drafted follow-up emails (E2)
@router.post("/pipeline/followups/{follow_up_id}/channel", response_model=None)
def followup_channel(
    request: Request,
    follow_up_id: PathId,
    channel: str = Form(""),
    next: str = Form(""),  # noqa: A002
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    """Change HOW a follow-up is planned to be done (WhatsApp / LinkedIn / Email / Call); only Email ones get AI drafts."""
    next_url = _safe_next(next)

    def run() -> Response:
        fu = followups_service.set_planned_channel(db, follow_up_id, channel, actor=actor_of(member))
        return _followup_response(request, db, fu, f"{fu.kind_label} will be done via {fu.planned_channel_label}.", next_url)

    return crm_guard(request, next_url or "/pipeline", run, db)


def _llm_fatal(db: Session, exc: Exception) -> DomainError:
    db.commit()  # keep the error llm_calls row
    return DomainError("llm_unavailable", f"The AI service refused the request ({type(exc).__name__}) - check the API key and model in .env.", 502)


@router.post("/pipeline/followups/{follow_up_id}/draft", response_model=None)
def followup_draft(
    request: Request,
    follow_up_id: PathId,
    next: str = Form(""),  # noqa: A002
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
    llm: LLMClient = Depends(get_llm),
) -> Response:
    """The per-follow-up "Draft email" button: ONE Haiku call -> a draft in the outbox. Nothing is approved or sent."""
    from sales0.db.models import FollowUp

    back = _safe_back(next, "/pipeline")

    def run() -> Response:
        f = db.get(FollowUp, follow_up_id)
        if f is None:
            raise DomainError("not_found", "That follow-up no longer exists.", 404)
        try:
            msg = followup_drafts_service.draft_follow_up(db, llm, follow_up_id, member)
        except (LLMAuthError, LLMModelNotFound) as exc:
            raise _llm_fatal(db, exc) from exc
        url = f"/outreach/messages/{msg.id}"
        if wants_json(request):
            return envelope_ok({"message_id": msg.id, "url": url}, 201)
        return crm_done(request, url, "Follow-up draft written - review it, then approve it. Nothing is sent until you do.")

    return crm_guard(request, back, run, db)


@router.post("/pipeline/followups/draft-due", response_model=None)
def followups_draft_due(
    request: Request,
    next: str = Form(""),  # noqa: A002
    prospect_id: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
    llm: LLMClient = Depends(get_llm),
) -> Response:
    """"Draft due follow-ups": drafts for the due Email follow-ups, at most SALES0_FOLLOWUP_DRAFT_BATCH per click (click
    again for the rest). Skipped ones (replied, unsubscribed, ...) are counted with their reasons in the flash."""
    back = _safe_back(next, "/outreach")

    def run() -> Response:
        try:
            result = followup_drafts_service.draft_due(db, llm, member, prospect_id=opt_int(prospect_id, "prospect"))
        except (LLMAuthError, LLMModelNotFound) as exc:
            raise _llm_fatal(db, exc) from exc
        if wants_json(request):
            return envelope_ok({
                "drafted": result.drafted, "skipped": result.skipped, "failed": result.failed, "remaining": result.remaining,
                "stopped": result.stopped,
                "outcomes": [{"follow_up_id": o.follow_up_id, "prospect": o.prospect_name, "status": o.status, "reason": o.reason,
                              "message_id": o.message_id} for o in result.outcomes],
            })
        return crm_done(request, back, result.summary())

    return crm_guard(request, back, run, db)


def _lead_action(request: Request, db: Session, lead_id: PathId, action: Callable[[], str], *, next_url: str | None = None) -> Response:
    from sales0.db.models import Lead

    row = db.get(Lead, lead_id)
    cid = (row.company_id or db.scalar(select_(Prospect_.company_id).where(Prospect_.id == row.prospect_id))) if row else None
    back = next_url or (f"/companies/{cid}" if cid else "/pipeline")

    def run() -> Response:
        message = action()
        if wants_json(request):
            return _json(leads_service.get_lead(db, lead_id))
        return crm_done(request, back, message)

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


def _client_action(request: Request, db: Session, client_id: PathId, action: Callable[[], str]) -> Response:
    from sales0.db.models import Client

    row = db.get(Client, client_id)
    back = f"/companies/{row.company_id}" if row is not None and row.company_id else "/pipeline"

    def run() -> Response:
        message = action()
        if wants_json(request):
            return _json(clients_service.get_client(db, client_id))
        return crm_done(request, back, message)

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
        return crm_done(request, f"/companies/{view.company_id}" if view.company_id else "/pipeline?stage=client",
                        f"Added client {view.name}.")

    return crm_guard(request, "/pipeline?stage=client", run, db)


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
