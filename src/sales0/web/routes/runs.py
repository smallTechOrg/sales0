"""Runs page (slice G): Run now + live progress (htmx polling), history, run detail."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.engine import get_db
from sales0.services import runs as runs_service
from sales0.services.views import RunView
from sales0.web.context import cost_badge_oob, is_htmx, redirect, render_html, render_page
from sales0.web.envelope import ok

router = APIRouter()

ACTIVE_STATUSES = frozenset({"queued", "running"})

ABORT_LABELS = {
    "checkpoint": "LinkedIn checkpoint detected",
    "captcha": "LinkedIn captcha detected",
    "logout": "LinkedIn logged out (the session expired)",
    "rate_limited": "LinkedIn rate limit detected",
}


def outcome_view(run: RunView, *, last: bool = False) -> dict[str, str] | None:
    """The one-line outcome for a finished run (None while it is still active).

    ``last=True`` phrases it as the previous run's outcome ("Last run finished — …") for a
    fresh /runs load after the run ended."""
    if run.status in ACTIVE_STATUSES:
        return None
    subject = "Last run" if last else "Run"
    if run.status == "succeeded":
        noun = "enquiry" if run.posts_new == 1 else "enquiries"
        text = f"{subject} finished — {run.posts_new} new {noun}"
        if run.abort_reason == "daily_post_cap":
            text += ". Daily post limit reached — the remaining keywords were skipped"
        return {"kind": "ok", "text": text}
    if run.status == "aborted":
        label = ABORT_LABELS.get(run.abort_reason or "", f"LinkedIn safety stop ({run.abort_reason or 'unknown'})")
        return {
            "kind": "error",
            "text": f"{subject} stopped: {label}",
            "guidance": "Posts saved before the stop were kept and triaged. Open LinkedIn in a normal browser, "
            "resolve it, then press Connect LinkedIn in Setup before running again.",
        }
    message = run.error_message or "The run failed — details are in the server log."
    return {"kind": "error", "text": f"Last run failed: {message}" if last else message}


def _caps() -> dict[str, int]:
    s = get_settings()
    return {"max_keywords": s.scrape_max_keywords_per_run, "max_posts": s.scrape_max_posts_per_keyword}


def _panel_ctx(db: Session, run: RunView | None, last_run: RunView | None = None) -> dict:
    """``run`` is the run this panel tracks (data-run-id, polling); ``last_run`` is only
    shown as a "Last run …" outcome on a fresh idle page and never sets data-run-id, so a
    client waiting for the panel to pick up a NEW run is not fooled by the old one."""
    active = run is not None and run.status in ACTIVE_STATUSES
    if run is not None:
        outcome = outcome_view(run)
    elif last_run is not None:
        outcome = outcome_view(last_run, last=True)
    else:
        outcome = None
    return {
        "run": run,
        "last_run": last_run if run is None else None,
        "active": active,
        "outcome": outcome,
        "eligibility": None if active else runs_service.run_eligibility(db),
        "caps": _caps(),
    }


def _history_html(request: Request, db: Session, *, oob: bool = False) -> str:
    return render_html(request, "runs/_history.html", {"runs": runs_service.list_runs(db), "oob": oob})


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    history = runs_service.list_runs(db)
    active = runs_service.active_run(db)
    ctx = _panel_ctx(db, active, last_run=history[0] if active is None and history else None)
    ctx["runs"] = history
    return render_page(request, db, "runs/page.html", ctx)


@router.post("/runs", response_model=None)
def run_now(request: Request, db: Session = Depends(get_db)) -> Response:
    run = runs_service.start_run(db)
    if not is_htmx(request):
        return redirect("/runs")
    panel = render_html(request, "runs/_panel.html", _panel_ctx(db, run))
    return HTMLResponse(panel + _history_html(request, db, oob=True), headers={"HX-Trigger": "session-changed"})


@router.get("/runs/{run_id}/progress", response_class=HTMLResponse)
def run_progress(request: Request, run_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    run = runs_service.get_run(db, run_id).run
    ctx = _panel_ctx(db, run)
    # The history table refreshes out-of-band on every poll so the active row's counters
    # (Keywords 1/3, Seen, New, …) move with the panel instead of staying "Queued 0/0".
    body = render_html(request, "runs/_panel.html", ctx) + cost_badge_oob(db) + _history_html(request, db, oob=True)
    if ctx["active"]:
        return HTMLResponse(body)
    # Final render: polling stops (no hx-trigger); the header status light refreshes once.
    return HTMLResponse(body, headers={"HX-Trigger": "session-changed"})


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    detail = runs_service.get_run(db, run_id)
    return render_page(
        request,
        db,
        "runs/detail.html",
        {"detail": detail, "run": detail.run, "outcome": outcome_view(detail.run), "active": detail.run.status in ACTIVE_STATUSES},
    )


@router.get("/api/runs/{run_id}")
def api_run(run_id: int, db: Session = Depends(get_db)) -> JSONResponse:
    return ok(runs_service.get_run(db, run_id).model_dump(mode="json"))


__all__ = ["router", "outcome_view"]
