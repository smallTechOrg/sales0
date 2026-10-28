"""``/tracked`` - tracked LinkedIn profiles, Run watch now (live progress), track/untrack/pause."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.engine import get_db
from sales0.services import inbox as inbox_service
from sales0.services import tracked_profiles as tracked
from sales0.services.runs import ACTIVE_STATUSES, get_run
from sales0.web.acting import clear_flash, pop_flash, set_flash
from sales0.web.context import is_htmx, render_html, render_page

router = APIRouter()


def _caps() -> dict[str, float | int]:
    s = get_settings()
    return {"max_profiles": s.watch_max_profiles_per_run, "min_days": f"{s.watch_min_days_between_checks:g}",
            "max_posts": s.watch_max_posts_per_profile}


def _panel_ctx(db: Session, run=None) -> dict:  # noqa: ANN001
    run = run or tracked.latest_watch_run(db)
    active = run is not None and run.status in ACTIVE_STATUSES
    error, due = (None, tracked.due_count(db)) if active else tracked.watch_eligibility(db)
    return {
        "run": run, "active": active, "error": error, "due": due, "caps": _caps(),
        "log": tracked.run_log(db, run.id) if run is not None else [],
    }


@router.get("/tracked", response_class=HTMLResponse)
def tracked_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    flash = pop_flash(request)
    ctx = {"profiles": tracked.list_tracked(db), "flash": flash, **_panel_ctx(db)}
    response = render_page(request, db, "tracked/page.html", ctx)
    if flash:
        clear_flash(response)
    return response


@router.post("/tracked/run", response_model=None)
def run_watch(request: Request, db: Session = Depends(get_db)) -> Response:
    run = tracked.start_watch_run(db)
    if not is_htmx(request):
        return RedirectResponse("/tracked", status_code=303)
    return HTMLResponse(render_html(request, "tracked/_panel.html", _panel_ctx(db, run)))


@router.get("/tracked/runs/{run_id}/progress", response_class=HTMLResponse)
def watch_progress(request: Request, run_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    run = get_run(db, run_id).run
    ctx = _panel_ctx(db, run)
    return HTMLResponse(render_html(request, "tracked/_panel.html", ctx), headers={} if ctx["active"] else {"HX-Trigger": "session-changed"})


def _back(message: str) -> RedirectResponse:
    response = RedirectResponse("/tracked", status_code=303)
    set_flash(response, message, "ok")
    return response


@router.post("/tracked/{profile_id}/untrack")
def untrack(profile_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    tracked.untrack(db, profile_id)
    db.commit()
    return _back("Profile untracked.")


@router.post("/tracked/{profile_id}/track")
def track(profile_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    tracked.track(db, profile_id)
    db.commit()
    return _back("Profile tracked.")


@router.post("/tracked/{profile_id}/pause")
def pause(profile_id: int, paused: str = Form("1"), db: Session = Depends(get_db)) -> RedirectResponse:
    tracked.set_paused(db, profile_id, paused == "1")
    db.commit()
    return _back("Profile paused." if paused == "1" else "Profile resumed.")


def _toggle(request: Request, db: Session, enquiry_id: int, on: bool) -> HTMLResponse:
    if on:
        tracked.track_author_of_enquiry(db, enquiry_id)
    else:
        card = inbox_service.get_card(db, enquiry_id)
        if card.author_profile_id is not None:
            tracked.untrack(db, card.author_profile_id)
    db.commit()
    return HTMLResponse(render_html(request, "tracked/_track_button.html", {"card": inbox_service.get_card(db, enquiry_id)}))


@router.post("/tracked/enquiry/{enquiry_id}/track", response_class=HTMLResponse)
def track_enquiry_author(request: Request, enquiry_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    return _toggle(request, db, enquiry_id, True)


@router.post("/tracked/enquiry/{enquiry_id}/untrack", response_class=HTMLResponse)
def untrack_enquiry_author(request: Request, enquiry_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    return _toggle(request, db, enquiry_id, False)


__all__ = ["router"]
