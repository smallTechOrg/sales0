"""Daily digest pages (Phase 4): latest digest + history, build now, copy, Gmail draft to self.

Nothing here sends email: the Gmail button only creates a DRAFT in the acting member's own
mailbox, addressed to themselves. Domain errors are shown as a red flash for browser form
posts; htmx and JSON callers get the standard envelope.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from sales0.db.engine import get_db
from sales0.db.models import TeamMember
from sales0.errors import DomainError
from sales0.services import digest as digest_service
from sales0.services.team import connections as connections_service
from sales0.web.acting import clear_flash, get_acting_member, pop_flash, set_flash
from sales0.web.context import PathId
from sales0.web.context import is_htmx, render_page
from sales0.web.envelope import wants_json

router = APIRouter()


def _flash_redirect(path: str, message: str, kind: str = "ok") -> RedirectResponse:
    response = RedirectResponse(path, status_code=303)
    set_flash(response, message, kind)
    return response


def _guard(request: Request, back: str, action: Callable[[], tuple[str, str]]) -> Response:
    """Run ``action`` -> (redirect path, success flash)."""
    try:
        path, message = action()
    except DomainError as exc:
        if wants_json(request) or is_htmx(request):
            raise
        return _flash_redirect(back, exc.message, "error")
    return _flash_redirect(path, message)


def _page(request: Request, db: Session, ctx: dict[str, Any]) -> HTMLResponse:
    flash = pop_flash(request)
    response = render_page(request, db, "digest/page.html", {**ctx, "flash": flash})
    if flash:
        clear_flash(response)
    return response


def _ctx(db: Session, digest: digest_service.DigestView | None) -> dict[str, Any]:
    history = digest_service.list_digests(db, 30)
    return {
        "digest": digest,
        "text": digest_service.digest_text(digest) if digest else "",
        "history": history,
        "item_text": digest_service.item_text,
        "item_href": digest_service.item_href,
    }


@router.get("/digest", response_class=HTMLResponse)
def digest_latest(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _page(request, db, _ctx(db, digest_service.latest_digest(db)))


@router.get("/digest/{digest_id}", response_class=HTMLResponse)
def digest_detail(digest_id: PathId, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _page(request, db, _ctx(db, digest_service.get_digest(db, digest_id)))


@router.post("/digest/build")
def digest_build(request: Request, db: Session = Depends(get_db)) -> Response:
    def action() -> tuple[str, str]:
        view = digest_service.generate_digest(db, trigger="manual")
        extra = f" {view.notes[0]}" if view.notes else ""
        return f"/digest/{view.id}", f"Digest for {view.digest_date.isoformat()} built.{extra}"

    return _guard(request, "/digest", action)


@router.post("/digest/{digest_id}/gmail-draft")
def digest_gmail_draft(
    digest_id: PathId,
    request: Request,
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
) -> Response:
    back = f"/digest/{digest_id}"

    def action() -> tuple[str, str]:
        digest_service.create_gmail_draft_to_self(db, digest_id, member)
        status = connections_service.connection_status(db, member.id)
        return back, f"A Gmail draft was created in {status.email or member.email}. Open Gmail Drafts to review it - nothing was sent."

    return _guard(request, back, action)
