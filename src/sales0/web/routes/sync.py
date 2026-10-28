"""``/sync`` — Google Sheet sync status, "Sync now", the opt-in switch and a read-only dry-run preview.

Counts only: nothing here ever shows a cell value. The page reads the persisted last run
(``sheet_sync_state`` via ``SqlStateStore``) plus the live runner status; a dry run is computed
in memory and never written anywhere (not even ``sheet_sync_state``).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.engine import get_db, get_sessionmaker
from sales0.errors import DomainError
from sales0.jobs.scheduler import SheetSyncJob, get_scheduler
from sales0.web.acting import clear_flash, pop_flash, set_flash
from sales0.web.context import is_htmx, render_fragment, render_page, toast_html
from sales0.services.sheetsync import SqlStateStore
from sales0.web.envelope import ok

router = APIRouter()

JOB = "sheet_sync"
_dry_lock = threading.Lock()
_last_dry_run: dict[str, Any] | None = None  # in memory only; a dry run never persists


@dataclass
class SyncPanel:
    available: bool
    job_enabled: bool
    interval_min: int
    running: bool
    state: str  # idle | running | disabled
    next_run_at: datetime | None = None
    next_run_in_min: int | None = None
    last_status: str | None = None
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_duration_s: float | None = None
    last_error: str | None = None
    last_dry_run: bool = False
    runs_total: int = 0
    tabs: list[dict[str, Any]] = field(default_factory=list)


def _tab_rows(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = []
    for name, t in ((report or {}).get("tabs") or {}).items():
        rows.append({
            "tab": name, "kind": t.get("kind"), "rows": t.get("rows", 0), "added": t.get("added", 0),
            "updated": t.get("updated", 0), "unchanged": t.get("unchanged", 0), "removed": t.get("removed", 0),
            "errors": t.get("error_count", len(t.get("errors") or [])),
            "warnings": t.get("warning_count", len(t.get("warnings") or [])),
            "fetch_error": t.get("fetch_error"), "removal_blocked": t.get("removal_blocked"),
        })
    return rows


def _job():  # type: ignore[no-untyped-def]
    job = get_scheduler().defs.get(JOB)
    if not isinstance(job, SheetSyncJob):
        raise DomainError("not_found", "The sheet sync job is not registered.", 404)
    return job


def build_panel(now: datetime | None = None) -> SyncPanel:
    scheduler = get_scheduler()
    job = _job()
    now = now or datetime.now(UTC)
    available = job.available()
    enabled = False
    for view in scheduler.snapshot():
        if view.name == JOB:
            enabled = view.enabled
    interval = scheduler.settings.sheet_sync_minutes
    runner = job.runner(scheduler.ctx) if available else None
    rs = runner.status() if runner is not None else None
    running = bool(rs and rs.running)
    saved = SqlStateStore(get_sessionmaker()).load()
    panel = SyncPanel(
        available=available, job_enabled=enabled, interval_min=interval, running=running,
        state="running" if running else ("disabled" if interval <= 0 else "idle"),
    )
    if rs and rs.next_run_at and enabled and interval > 0 and not running:
        nxt = rs.next_run_at if rs.next_run_at.tzinfo else rs.next_run_at.replace(tzinfo=UTC)
        panel.next_run_at = nxt
        panel.next_run_in_min = max(0, round((nxt - now).total_seconds() / 60))
    if saved:
        panel.last_status = saved["last_status"]
        panel.last_started_at = saved["last_started_at"]
        panel.last_finished_at = saved["last_finished_at"]
        panel.last_duration_s = saved["last_duration_s"]
        panel.last_error = saved["last_error"]
        panel.last_dry_run = bool(saved["last_dry_run"])
        panel.runs_total = saved["runs_total"] or 0
        panel.tabs = _tab_rows(saved["report"])
        if panel.last_error is None and saved["last_status"] == "partial":
            panel.last_error = "One or more tabs could not be read - see the Problem column."
    return panel


def _status_context() -> dict[str, Any]:
    return {"panel": build_panel(), "dry": _last_dry_run, "interval": get_settings().sheet_sync_minutes}


@router.get("/sync", response_class=HTMLResponse)
def sync_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    flash = pop_flash(request)
    response = render_page(request, db, "sync/page.html", {**_status_context(), "flash": flash})
    if flash:
        clear_flash(response)
    return response


@router.get("/sync/status", response_class=HTMLResponse)
def sync_status(request: Request) -> HTMLResponse:
    return render_fragment(request, "sync/_status.html", _status_context())


def _respond(request: Request, message: str, kind: str = "ok") -> Response:
    if is_htmx(request):
        html = render_fragment(request, "sync/_status.html", _status_context()).body.decode()
        return HTMLResponse(html + toast_html(message, kind))
    response = RedirectResponse("/sync", status_code=303)
    set_flash(response, message, kind)
    return response


@router.post("/sync/run")
def sync_run(request: Request) -> Response:
    job = _job()
    if not job.available():
        raise DomainError("sync_unavailable", "The CRM sheet repository is not installed.", 409)
    if not job.runner(get_scheduler().ctx).trigger_now():
        raise DomainError("sync_running", "A sync is already running.", 409)
    return _respond(request, "Sync started.")


@router.post("/sync/enable")
def sync_enable(request: Request) -> Response:
    """One-click opt-in of the interval job (the same switch as /schedule)."""
    if get_settings().sheet_sync_minutes <= 0:
        raise DomainError("sync_disabled", "SALES0_SHEET_SYNC_MINUTES is 0, so the interval is off.", 409)
    get_scheduler().set_enabled(JOB, True)
    return _respond(request, "Automatic sync enabled.")


@router.post("/sync/disable")
def sync_disable(request: Request) -> Response:
    get_scheduler().set_enabled(JOB, False)
    return _respond(request, "Automatic sync disabled.")


def _run_dry() -> dict[str, Any]:
    from sales0.jobs import sheet_sync_job as sj
    from sales0.services.sheetsync import SheetFetcher, SyncEngine, get_repository, make_client, tabs_from_settings

    settings = get_settings()
    repo = get_repository()
    if repo is None:
        raise DomainError("sync_unavailable", sj.NO_REPOSITORY, 409)
    fetcher = None
    if sj._source_factory is not None:
        source = sj._source_factory()
    else:
        fetcher = SheetFetcher(
            make_client(timeout_s=settings.sheet_fetch_timeout_s),
            retries=settings.sheet_fetch_retries, max_bytes=settings.sheet_fetch_max_bytes,
        )
        source = fetcher
    try:
        engine = SyncEngine(source, repo, tabs_from_settings(settings), today=lambda: datetime.now(settings.tz).date())
        report = engine.run(dry_run=True)
    finally:
        if fetcher is not None:
            fetcher.close()
    return {"at": datetime.now(UTC), "tabs": _tab_rows(report.to_dict()), "ok": report.ok, "total_changes": report.total_changes}


@router.post("/sync/dry-run")
def sync_dry_run(request: Request) -> Response:
    """Fetch and diff without writing anything; the result is shown on the page (in memory only)."""
    global _last_dry_run
    if not _dry_lock.acquire(blocking=False):
        raise DomainError("sync_running", "A preview is already running.", 409)
    try:
        _last_dry_run = _run_dry()
    finally:
        _dry_lock.release()
    return _respond(request, "Preview ready - nothing was written.")


@router.get("/api/sync/status")
def api_sync_status() -> Response:
    p = build_panel()
    return ok({
        "enabled": p.job_enabled and p.interval_min > 0,
        "interval_min": p.interval_min,
        "running": p.running,
        "next_run_at": p.next_run_at.isoformat() if p.next_run_at else None,
        "next_run_in_min": p.next_run_in_min,
        "last_started_at": p.last_started_at.isoformat() if p.last_started_at else None,
        "last_finished_at": p.last_finished_at.isoformat() if p.last_finished_at else None,
        "last_status": p.last_status,
        "last_error": p.last_error,
        "tabs": [
            {k: t[k] for k in ("tab", "kind", "rows", "added", "updated", "unchanged", "removed", "errors", "warnings", "fetch_error")}
            for t in p.tabs
        ],
    })
