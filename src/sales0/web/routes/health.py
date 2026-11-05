"""GET /api/health — liveness + DB revision + configuration presence (never secret values); GET /health — the Health
page (every moving part and what to do when one is not working; behind the login)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.engine import get_db, get_engine
from sales0.db.migrate_check import current_revision
from sales0.logging import get_logger
from sales0.web.envelope import err, ok

router = APIRouter()
log = get_logger("sales0.web.health")


@router.get("/api/health")
def health(request: Request) -> JSONResponse:
    settings = get_settings()
    try:
        revision = current_revision(get_engine())
    except Exception as exc:  # noqa: BLE001 — health must answer, not crash
        log.error("health_db_error", error_type=type(exc).__name__, exc_info=exc)
        return err("db_unavailable", "The database is unavailable — check SALES0_DATABASE_URL and the server log.", 503)
    # Unauthenticated callers (logins enabled, no valid session) learn only that the server is up.
    if getattr(request.state, "auth_enabled", True) and getattr(request.state, "login_member_id", None) is None:
        return ok({"status": "ok"})
    return ok(
        {
            "status": "ok",
            "db_revision": revision,
            "linkedin_mode": settings.linkedin_mode,
            "model": settings.llm_model,
            "api_key_present": settings.api_key_present,
        }
    )


@router.get("/health", response_class=HTMLResponse)
def health_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    from sales0.services import health_report
    from sales0.web.context import render_page

    return render_page(request, db, "health/page.html", {"checks": health_report.report(db)})
