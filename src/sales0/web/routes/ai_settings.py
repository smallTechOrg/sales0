"""SMA-48: GET/POST /settings/ai - the admin's AI provider settings; POST /settings/ai/test - try them first."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from sales0.db.engine import get_db
from sales0.db.models import TeamMember
from sales0.errors import DomainError
from sales0.services import ai_settings
from sales0.web.acting import clear_flash, pop_flash, require_admin, set_flash
from sales0.web.context import render_page

router = APIRouter()
FIELDS = ("provider", "base_url", "model", "research_model", "api_key", "clear_key", "price_in", "price_out")


async def _form(request: Request) -> dict[str, Any]:
    raw = await request.form()
    return {k: str(raw.get(k) or "") for k in FIELDS}


def _render(request: Request, db: Session, *, form: dict[str, Any] | None = None, result: Any = None,
            error: str | None = None, status_code: int = 200) -> Response:  # fmt: skip
    flash = pop_flash(request)
    current = ai_settings.view(db)
    values = {
        "provider": current.provider, "base_url": current.base_url, "model": current.model,
        "research_model": current.research_model,
        "price_in": f"{current.price_in:g}" if current.price_in else "", "price_out": f"{current.price_out:g}" if current.price_out else "",
    }  # fmt: skip
    if form is not None:
        values.update({k: v for k, v in form.items() if k not in ("api_key", "clear_key")})  # the key is never echoed
    ctx = {"current": current, "values": values, "presets": ai_settings.PRESETS, "result": result, "error": error,
           "flash": flash}  # fmt: skip
    response = render_page(request, db, "settings/ai.html", ctx, status_code=status_code, headers={"Cache-Control": "no-store"})
    if flash:
        clear_flash(response)
    return response


@router.get("/settings/ai", response_class=HTMLResponse)
def ai_settings_page(request: Request, db: Session = Depends(get_db), admin: TeamMember = Depends(require_admin)) -> Response:
    return _render(request, db)


@router.post("/settings/ai")
async def save_ai_settings(request: Request, db: Session = Depends(get_db), admin: TeamMember = Depends(require_admin)) -> Response:
    form = await _form(request)
    try:
        cfg = ai_settings.save(db, form, admin)
    except DomainError as exc:
        return _render(request, db, form=form, error=exc.message, status_code=422)
    db.commit()
    response = RedirectResponse("/settings/ai", status_code=303)
    set_flash(response, f"Saved: the app now uses {cfg.label} ({cfg.model}). Press Test to check it answers.", "ok")
    return response


@router.post("/settings/ai/test", response_class=HTMLResponse)
async def test_ai_settings(request: Request, db: Session = Depends(get_db), admin: TeamMember = Depends(require_admin)) -> Response:
    form = await _form(request)
    try:
        cfg = ai_settings.config_from_form(db, form)
    except DomainError as exc:
        return _render(request, db, form=form, error=exc.message, status_code=422)
    return _render(request, db, form=form, result=ai_settings.run_test(cfg))
