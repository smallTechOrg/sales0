"""Prospect page: Research (versioned, editable, sources), the AI account summary (versioned, editable) and our own
notes (dated, authored; add / edit / delete own). See ``spec/capabilities/prospect-research.md``.

AI work runs INSIDE the request (hosted mode has no background worker for it) with an htmx spinner: research is
capped at ~60 s of website reading plus one web-search request; the summary is one call. Each mutating route answers
htmx (the card re-rendered out-of-band + a toast), plain forms (303 + flash) and JSON (the envelope).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from sales0.agent.research import SafeFetcher
from sales0.config import get_settings
from sales0.db.engine import get_db
from sales0.db.models import TeamMember
from sales0.errors import DomainError
from sales0.llm import LLMAuthError, LLMClient, LLMError, LLMModelNotFound, get_llm_client
from sales0.services.crm import notes as notes_service
from sales0.services.research import context, docs, linkedin_page
from sales0.services.research import prospect as research_service
from sales0.services.research import summary as summary_service
from sales0.web.acting import get_acting_member
from sales0.web.context import PathId, actor_of, cost_badge_oob, flash_redirect, is_htmx, render_html, toast_html
from sales0.web.envelope import ok as envelope_ok
from sales0.web.envelope import wants_json

router = APIRouter()


def get_llm() -> LLMClient:
    """LLM seam (``app.dependency_overrides[get_llm]`` in tests)."""
    return get_llm_client()


def _fetcher() -> SafeFetcher | None:
    hosts = get_settings().research_private_hosts  # [] unless fixture mode + the explicit test switch
    return SafeFetcher(allow_private_hosts=hosts) if hosts else None


# ---------------------------------------------------------------------- page context
def research_ctx(db: Session, prospect_id: int, member: TeamMember | None) -> dict[str, Any]:
    """Everything the three cards need (also used by the prospect page itself)."""
    b = context.basics(db, prospect_id)
    research = docs.history(db, prospect_id, "research")
    summary = docs.history(db, prospect_id, "summary")
    return {
        "pid": prospect_id,
        "research": research[0] if research else None,
        "research_history": research,
        "summary": summary[0] if summary else None,
        "summary_history": summary,
        "notes": notes_service.list_notes(db, prospect_id, member),
        "li_status": linkedin_page.status_for(db, prospect_id, b.company_linkedin_url),
        "web_search_on": get_settings().research_web_search,
        "has_website": bool(b.website),
        "sheet_notes": _sheet_notes(db, prospect_id),
    }


def _sheet_notes(db: Session, prospect_id: int) -> str | None:
    from sales0.db.models import Prospect

    p = db.get(Prospect, prospect_id)
    return p.sheet_notes if p is not None and p.sheet_notes else None


CARDS = {
    "research": "pipeline/_research.html",
    "summary": "pipeline/_summary.html",
    "notes": "pipeline/_notes.html",
}


def _answer(
    request: Request, db: Session, prospect_id: int, member: TeamMember | None, cards: tuple[str, ...], message: str,
    kind: str = "ok", data: Any = None, cost: bool = False,
) -> Response:
    if wants_json(request):
        return envelope_ok(data if data is not None else {"message": message})
    if is_htmx(request):
        ctx = research_ctx(db, prospect_id, member)
        body = "".join(render_html(request, CARDS[c], {**ctx, "oob": True}) for c in cards)
        return HTMLResponse(body + (cost_badge_oob(db) if cost else "") + toast_html(message, kind))
    return flash_redirect(f"/pipeline/prospects/{prospect_id}#{cards[0]}-card", message, "ok" if kind == "ok" else "error")


def _guard(request: Request, db: Session, prospect_id: int, action: Callable[[], Response]) -> Response:
    try:
        return action()
    except DomainError as exc:
        db.rollback()
        if wants_json(request) or is_htmx(request):
            raise
        return flash_redirect(f"/pipeline/prospects/{prospect_id}", exc.message, "error")


def _llm_errors(db: Session, exc: Exception) -> DomainError:
    if isinstance(exc, LLMAuthError | LLMModelNotFound):
        db.commit()  # keep the error llm_calls row
        return DomainError(
            "llm_unavailable", f"The AI service refused the request ({type(exc).__name__}) - check the API key and model in .env.", 502
        )
    return DomainError("research_failed", "The AI service is unavailable right now. Try again in a minute.", 502)


# ---------------------------------------------------------------------- research
@router.post("/pipeline/prospects/{prospect_id}/research", response_model=None)
def run_research(
    request: Request,
    prospect_id: PathId,
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
    llm: LLMClient = Depends(get_llm),
) -> Response:
    def act() -> Response:
        fetcher = _fetcher()
        try:
            doc, li_note = research_service.research_prospect(db, llm, member, prospect_id, actor=actor_of(member), fetcher=fetcher)
        except LLMError as exc:
            raise _llm_errors(db, exc) from exc
        finally:
            if fetcher is not None:
                fetcher.close()
        db.commit()
        status = {"done": "Research finished", "partial": "Research finished with limited information", "failed": "Research found little"}
        message = f"{status.get(doc.status, 'Research finished')} - version {doc.version}. {li_note}".strip()
        return _answer(request, db, prospect_id, member, ("research",), message, "ok" if doc.status != "failed" else "error",
                       doc.model_dump(mode="json"), cost=True)

    return _guard(request, db, prospect_id, act)


@router.post("/pipeline/prospects/{prospect_id}/research/edit", response_model=None)
def edit_research(
    request: Request,
    prospect_id: PathId,
    what_they_do: str = Form(""),
    size_hints: str = Form(""),
    suggested_offering: str = Form(""),
    ai_training_needs: str = Form(""),
    personalisation_hooks: str = Form(""),
    sources: str = Form(""),
    enquiry_insights: str | None = Form(None),
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
) -> Response:
    def act() -> Response:
        doc = research_service.edit_research(
            db, prospect_id, actor=actor_of(member), what_they_do=what_they_do, size_hints=size_hints,
            suggested_offering=suggested_offering, ai_training_needs=ai_training_needs,
            personalisation_hooks=personalisation_hooks, sources=sources, enquiry_insights=enquiry_insights,
        )  # fmt: skip
        db.commit()
        return _answer(request, db, prospect_id, member, ("research",), f"Research saved as version {doc.version}.",
                       data=doc.model_dump(mode="json"))

    return _guard(request, db, prospect_id, act)


# ---------------------------------------------------------------------- account summary
@router.post("/pipeline/prospects/{prospect_id}/summary", response_model=None)
def refresh_summary(
    request: Request,
    prospect_id: PathId,
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
    llm: LLMClient = Depends(get_llm),
) -> Response:
    def act() -> Response:
        try:
            doc = summary_service.refresh_summary(db, llm, member, prospect_id, actor=actor_of(member))
        except LLMError as exc:
            raise _llm_errors(db, exc) from exc
        db.commit()
        if doc.status == "failed":
            return _answer(request, db, prospect_id, member, ("summary",), doc.error or "The summary failed - try again.", "error",
                           doc.model_dump(mode="json"), cost=True)
        return _answer(request, db, prospect_id, member, ("summary",), f"Account summary refreshed - version {doc.version}.",
                       data=doc.model_dump(mode="json"), cost=True)

    return _guard(request, db, prospect_id, act)


@router.post("/pipeline/prospects/{prospect_id}/summary/edit", response_model=None)
def edit_summary(
    request: Request,
    prospect_id: PathId,
    body: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
) -> Response:
    def act() -> Response:
        doc = summary_service.edit_summary(db, prospect_id, body, actor=actor_of(member))
        db.commit()
        return _answer(request, db, prospect_id, member, ("summary",), f"Summary saved as version {doc.version}.",
                       data=doc.model_dump(mode="json"))

    return _guard(request, db, prospect_id, act)


# ---------------------------------------------------------------------- our notes
@router.post("/pipeline/prospects/{prospect_id}/notes", response_model=None)
def add_note(
    request: Request,
    prospect_id: PathId,
    body: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
) -> Response:
    def act() -> Response:
        note = notes_service.add_note(db, prospect_id, body, actor=actor_of(member), member=member)
        db.commit()
        return _answer(request, db, prospect_id, member, ("notes",), "Note added.", data=note.model_dump(mode="json"))

    return _guard(request, db, prospect_id, act)


def _note_prospect(db: Session, note_id: int) -> int:
    from sales0.db.models import ProspectNote

    row = db.get(ProspectNote, note_id)
    if row is None:
        raise DomainError("not_found", "That note no longer exists - refresh the page.", 404)
    return row.prospect_id


@router.post("/pipeline/notes/{note_id}/edit", response_model=None)
def edit_note(
    request: Request,
    note_id: PathId,
    body: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
) -> Response:
    prospect_id = _note_prospect(db, note_id)

    def act() -> Response:
        note = notes_service.edit_note(db, note_id, body, member=member)
        db.commit()
        return _answer(request, db, prospect_id, member, ("notes",), "Note saved.", data=note.model_dump(mode="json"))

    return _guard(request, db, prospect_id, act)


@router.post("/pipeline/notes/{note_id}/delete", response_model=None)
def delete_note(
    request: Request,
    note_id: PathId,
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
) -> Response:
    prospect_id = _note_prospect(db, note_id)

    def act() -> Response:
        notes_service.delete_note(db, note_id, member=member)
        db.commit()
        return _answer(request, db, prospect_id, member, ("notes",), "Note deleted.", data={"deleted": note_id})

    return _guard(request, db, prospect_id, act)


__all__ = ["research_ctx", "router"]
