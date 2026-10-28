"""Suggestions page (Phase 4): AI-proposed keyword changes the team accepts or dismisses.

Same error handling as the outreach pages: a ``DomainError`` (including an exhausted spend
cap) is a red flash after redirect for plain form posts and the JSON/htmx envelope otherwise.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from sales0.agent.suggest import run_suggestions
from sales0.db.engine import get_db
from sales0.db.models import TeamMember
from sales0.errors import DomainError
from sales0.llm import LLMAuthError, LLMClient, LLMModelNotFound, get_llm_client
from sales0.logging import get_logger
from sales0.services import suggestions as service
from sales0.services.keyword_yield import keyword_yield
from sales0.web.acting import (
    clear_flash,
    get_acting_member_optional,
    pop_flash,
    set_flash,
)
from sales0.web.context import is_htmx, render_page
from sales0.web.envelope import wants_json

log = get_logger("sales0.suggestions.web")
router = APIRouter()
BACK = "/suggestions"


def get_llm() -> LLMClient:
    """LLM seam for this page (override with ``app.dependency_overrides`` in tests)."""
    return get_llm_client()


def _flash_redirect(path: str, message: str, kind: str = "ok") -> RedirectResponse:
    response = RedirectResponse(path, status_code=303)
    set_flash(response, message, kind)
    return response


def _guard(request: Request, back: str, action: Callable[[], str]) -> Response:
    try:
        message = action()
    except DomainError as exc:
        if wants_json(request) or is_htmx(request):
            raise
        return _flash_redirect(back, exc.message, "error")
    return _flash_redirect(back, message)


def _page(
    request: Request, db: Session, template: str, ctx: dict[str, Any]
) -> HTMLResponse:
    flash = pop_flash(request)
    response = render_page(request, db, template, {**ctx, "flash": flash})
    if flash:
        clear_flash(response)
    return response


@router.get("/suggestions", response_class=HTMLResponse)
def suggestions_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    pending = service.list_suggestions(db, "pending")
    decided = service.list_suggestions(db, ("accepted", "dismissed"), limit=15)
    return _page(
        request, db, "suggestions/page.html",
        {
            "pending": [_view(s) for s in pending], "decided": [_view(s) for s in decided],
            "yields": keyword_yield(db),
        },
    )  # fmt: skip


def _view(row: Any) -> dict[str, Any]:
    return {
        "row": row,
        "evidence": service.evidence_items(row) if row.kind == "new_keyword" else [],
        "stats": service.evidence_stats(row) if row.kind == "pause_keyword" else {},
    }


@router.post("/suggestions/generate")
def generate(
    request: Request, db: Session = Depends(get_db), llm: LLMClient = Depends(get_llm)
) -> Response:
    def action() -> str:
        try:
            batch = run_suggestions(db, llm)
        except (LLMAuthError, LLMModelNotFound) as exc:
            raise DomainError(
                "llm_unavailable", f"{exc} Check the AI settings.", 502
            ) from exc
        cost = (
            f" (AI cost ${batch.cost_usd_micros / 1_000_000:.4f})"
            if batch.llm_called
            else ""
        )
        return batch.message + cost

    return _guard(request, BACK, action)


@router.post("/suggestions/{suggestion_id}/accept")
def accept(
    suggestion_id: int, request: Request, db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:  # fmt: skip
    def action() -> str:
        decision = service.accept(db, suggestion_id, member)
        row = decision.suggestion
        done = (
            f"Added keyword '{row.text}'."
            if row.kind == "new_keyword"
            else f"Paused keyword '{row.text}'."
        )
        return f"{decision.note} Marked accepted." if decision.note else done

    return _guard(request, BACK, action)


@router.post("/suggestions/{suggestion_id}/dismiss")
def dismiss(
    suggestion_id: int, request: Request, db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:  # fmt: skip
    def action() -> str:
        service.dismiss(db, suggestion_id, member)
        return "Dismissed. It will not be suggested again."

    return _guard(request, BACK, action)
