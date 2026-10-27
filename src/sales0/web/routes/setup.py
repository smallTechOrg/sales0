"""Setup page (slice F): LinkedIn connection card, keywords, offerings, safety limits."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.engine import get_db
from sales0.errors import DomainError
from sales0.jobs.runner import get_runner
from sales0.services import keywords as keywords_service
from sales0.services import offerings as offerings_service
from sales0.services import session as session_service
from sales0.services.views import OfferingInput, OfferingView, SessionStatusView
from sales0.web.context import is_htmx, redirect, render_html, render_page, toast_html
from sales0.web.envelope import ok

router = APIRouter()

OFFERING_INVALID = "Offering name (1–80) and group (1–60) are required."


# ---------------------------------------------------------------------- helpers
def _status_key(session: SessionStatusView) -> str:
    return f"{session.status}:{session.busy_action or ''}"


def _safety_caps() -> list[tuple[str, str]]:
    s = get_settings()
    return [
        ("Keywords per run", f"{s.scrape_max_keywords_per_run}"),
        ("Posts per keyword", f"{s.scrape_max_posts_per_keyword}"),
        ("Posts per day", f"{s.scrape_max_posts_per_day}"),
        ("Runs per day", f"{s.scrape_max_runs_per_day}"),
        ("Delay between page actions", f"{s.scrape_delay_min_s:g}–{s.scrape_delay_max_s:g} s (random)"),
        ("Pause between keywords", f"{s.scrape_keyword_pause_min_s:g}–{s.scrape_keyword_pause_max_s:g} s (random)"),
        ("Scrolls per keyword", f"{s.scrape_max_scrolls}"),
        ("Post date filter", s.scrape_date_filter),
        ("Connect waits for login", f"{s.linkedin_connect_timeout_s} s"),
        ("Browser window during runs", "hidden (headless)" if s.linkedin_headless or s.linkedin_mode == "fixture" else "visible"),
    ]


def _offerings_ctx(db: Session) -> dict:
    """Active offerings grouped by group name, keeping the service's order (groups shown once)."""
    groups: dict[str, list[OfferingView]] = {}
    for offering in offerings_service.list_offerings(db):
        groups.setdefault(offering.group_name, []).append(offering)
    return {
        "offering_groups": list(groups.items()),
        "group_names": offerings_service.list_group_names(db),
    }


def _find_offering(db: Session, offering_id: int) -> OfferingView:
    for offering in offerings_service.list_offerings(db, include_inactive=True):
        if offering.id == offering_id:
            return offering
    raise DomainError("not_found", "That item no longer exists — refresh the page.", 404)


def _offering_input(name: str, group_name: str, description: str) -> OfferingInput:
    try:
        return OfferingInput(name=name.strip(), group_name=group_name.strip(), description=description.strip())
    except ValidationError as exc:
        raise DomainError("offering_invalid", OFFERING_INVALID, 400) from exc


def _html(*parts: str, headers: dict[str, str] | None = None) -> HTMLResponse:
    return HTMLResponse("".join(parts), headers=headers)


def _linkedin_card(request: Request, session: SessionStatusView, *, changed: bool) -> HTMLResponse:
    html = render_html(request, "setup/_linkedin_card.html", {"session": session})
    return _html(html, headers={"HX-Trigger": "session-changed"} if changed else None)


def _keyword_list_html(request: Request, db: Session) -> str:
    return render_html(request, "setup/_keyword_list.html", {"keywords": keywords_service.list_keywords(db)})


def _offering_list_html(request: Request, db: Session) -> str:
    return render_html(request, "setup/_offering_list.html", _offerings_ctx(db))


# ---------------------------------------------------------------------- page
@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return render_page(
        request,
        db,
        "setup/page.html",
        {
            "session": session_service.session_status(db),
            "keywords": keywords_service.list_keywords(db),
            "caps": _safety_caps(),
            **_offerings_ctx(db),
        },
    )


# ---------------------------------------------------------------------- keywords
@router.post("/setup/keywords", response_model=None)
def add_keyword(request: Request, text: str = Form(""), db: Session = Depends(get_db)) -> Response:
    keyword = keywords_service.add_keyword(db, text)
    if not is_htmx(request):
        return redirect("/setup")
    return _html(_keyword_list_html(request, db), toast_html(f"Added keyword '{keyword.text}'."))


@router.post("/setup/keywords/{keyword_id}/delete", response_model=None)
def remove_keyword(request: Request, keyword_id: int, db: Session = Depends(get_db)) -> Response:
    keywords_service.remove_keyword(db, keyword_id)
    if not is_htmx(request):
        return redirect("/setup")
    return _html(_keyword_list_html(request, db), toast_html("Keyword removed. Its run history is kept."))


@router.post("/setup/keywords/{keyword_id}/toggle", response_model=None)
def toggle_keyword(request: Request, keyword_id: int, active: str = Form("true"), db: Session = Depends(get_db)) -> Response:
    keyword = keywords_service.set_keyword_active(db, keyword_id, active.strip().lower() in {"true", "1", "on", "yes"})
    if not is_htmx(request):
        return redirect("/setup")
    return _html(render_html(request, "setup/_keyword_row.html", {"keyword": keyword}))


# ---------------------------------------------------------------------- offerings
@router.post("/setup/offerings", response_model=None)
def create_offering(
    request: Request,
    name: str = Form(""),
    group_name: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    offering = offerings_service.create_offering(db, _offering_input(name, group_name, description))
    if not is_htmx(request):
        return redirect("/setup")
    return _html(_offering_list_html(request, db), toast_html(f"Added offering '{offering.name}' to {offering.group_name}."))


@router.get("/setup/offerings/{offering_id}/edit", response_class=HTMLResponse)
def edit_offering(request: Request, offering_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    offering = _find_offering(db, offering_id)
    return _html(
        render_html(
            request,
            "setup/_offering_edit_row.html",
            {"offering": offering, "group_names": offerings_service.list_group_names(db)},
        )
    )


@router.post("/setup/offerings/{offering_id}", response_model=None)
def update_offering(
    request: Request,
    offering_id: int,
    name: str = Form(""),
    group_name: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    offering = offerings_service.update_offering(db, offering_id, _offering_input(name, group_name, description))
    if not is_htmx(request):
        return redirect("/setup")
    return _html(_offering_list_html(request, db), toast_html(f"Saved '{offering.name}'."))


@router.post("/setup/offerings/{offering_id}/delete", response_model=None)
def delete_offering(request: Request, offering_id: int, db: Session = Depends(get_db)) -> Response:
    offering = _find_offering(db, offering_id)
    result = offerings_service.delete_offering(db, offering_id)
    if result == "deactivated":
        uses = offering.enquiry_count
        noun = "enquiry" if uses == 1 else "enquiries"
        message = f"'{offering.name}' deactivated — still used by {uses} {noun}. It is hidden from triage and the inbox selects."
    else:
        message = f"Deleted '{offering.name}'."
    if not is_htmx(request):
        return redirect("/setup")
    return _html(_offering_list_html(request, db), toast_html(message))


# ---------------------------------------------------------------------- LinkedIn
@router.post("/setup/linkedin/connect", response_model=None)
def connect_linkedin(request: Request, db: Session = Depends(get_db)) -> Response:
    session = session_service.start_connect(db)
    if not is_htmx(request):
        return redirect("/setup")
    return _linkedin_card(request, session, changed=True)


@router.post("/setup/linkedin/connect/cancel", response_model=None)
def cancel_connect(request: Request, db: Session = Depends(get_db)) -> Response:
    get_runner().cancel_connect()
    if not is_htmx(request):
        return redirect("/setup")
    return _linkedin_card(request, session_service.session_status(db), changed=True)


@router.post("/setup/linkedin/check", response_model=None)
def check_linkedin(request: Request, db: Session = Depends(get_db)) -> Response:
    session = session_service.start_check(db)
    if not is_htmx(request):
        return redirect("/setup")
    return _linkedin_card(request, session, changed=True)


@router.get("/setup/linkedin/status", response_class=HTMLResponse)
def linkedin_status(request: Request, prev: str = "", db: Session = Depends(get_db)) -> HTMLResponse:
    session = session_service.session_status(db)
    return _linkedin_card(request, session, changed=bool(prev) and prev != _status_key(session))


@router.get("/api/session")
def api_session(db: Session = Depends(get_db)) -> JSONResponse:
    return ok(session_service.session_status(db).model_dump(mode="json"))


__all__ = ["router"]
