"""``/schedule`` — enable/disable each scheduled job, see next runs and last results, run now."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from sales0.db.engine import get_db
from sales0.errors import DomainError
from sales0.jobs.scheduler import get_scheduler
from sales0.web.acting import clear_flash, pop_flash, require_admin, set_flash
from sales0.web.context import render_page

router = APIRouter()


def _redirect(message: str, kind: str = "ok") -> RedirectResponse:
    response = RedirectResponse("/schedule", status_code=303)
    set_flash(response, message, kind)
    return response


def _known(name: str) -> None:
    if name not in get_scheduler().defs:
        raise DomainError("not_found", "That job does not exist.", 404)


@router.get("/schedule", response_class=HTMLResponse)
def schedule_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    scheduler = get_scheduler()
    flash = pop_flash(request)
    response = render_page(
        request, db, "schedule/page.html",
        {"jobs": scheduler.snapshot(), "flash": flash, "thread_running": scheduler.running,
         "timezone": scheduler.settings.timezone},
    )
    if flash:
        clear_flash(response)
    return response


@router.post("/schedule/{name}/enable", dependencies=[Depends(require_admin)])
def set_enabled(name: str, enabled: str = Form("1")) -> RedirectResponse:
    _known(name)
    on = enabled == "1"
    get_scheduler().set_enabled(name, on)
    return _redirect(f"{name.replace('_', ' ').capitalize()} {'enabled' if on else 'disabled'}.")


@router.post("/schedule/{name}/run", dependencies=[Depends(require_admin)])
def run_now(name: str) -> RedirectResponse:
    _known(name)
    result = get_scheduler().run_now(name)
    kind = "ok" if result.status == "ok" else "error"
    return _redirect(f"{name.replace('_', ' ').capitalize()}: {result.status}. {result.detail}".strip(), kind)
