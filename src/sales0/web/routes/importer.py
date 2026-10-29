"""Import page (Phase 2): paste a single post (one real triage call) and import an app-owned CSV.

The Google Sheets themselves are synced, not imported (see the Sync page). Services:
``services.importer.paste.paste_post`` and ``services.importer.csv_import.import_csv``.

Paste is slow (one Haiku call): the form uses htmx with a spinner; a plain form post works too.
A duplicate answers "Already in your inbox" with a link instead of creating anything.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from sales0.db.engine import get_db
from sales0.db.models import TeamMember
from sales0.errors import DomainError
from sales0.llm import LLMAuthError, LLMClient, LLMError, LLMModelNotFound, get_llm_client
from sales0.services import inbox as inbox_service
from sales0.services import importer
from sales0.services.importer import ImportBatchView
from sales0.services.importer.csv_import import MAX_BYTES
from sales0.web.acting import clear_flash, get_acting_member_optional, pop_flash, require_admin
from sales0.web.context import PathId
from sales0.web.context import actor_of, crm_done, crm_guard, opt_str, render_page
from sales0.web.envelope import ok as envelope_ok
from sales0.web.envelope import wants_json

router = APIRouter()

KINDS: tuple[tuple[str, str], ...] = (
    ("enquiries", "Enquiries"),
    ("discarded", "Discarded enquiries"),
    ("outreach", "Outreach"),
    ("prospects", "Prospects"),
    ("working", "Working sheet"),
    ("competitors", "Competitors"),
    ("groups", "Groups"),
    ("clients", "Legacy clients"),
    ("contacts", "Contacts"),
)
KIND_LABELS = {**dict(KINDS), "paste": "Pasted post"}
RECENT_BATCHES = 15
MAX_PASTE_CHARS = 20_000


def get_llm() -> LLMClient:
    """The LLM client for the paste route (same seam as triage / outreach; override in tests)."""
    return get_llm_client()


def batch_view(batch: ImportBatchView) -> dict[str, Any]:
    """Template-friendly summary of an ``ImportBatchView`` (row numbers and codes only, never cell content)."""
    return {
        "id": batch.id,
        "filename": batch.filename,
        "kind": batch.kind,
        "total": batch.rows_total,
        "new": batch.rows_new,
        "merged": batch.rows_merged,
        "unchanged": batch.rows_unchanged,
        "errors_count": batch.rows_error,
        "errors": [{"row": i.row, "code": i.code, "level": i.level} for i in batch.errors],
        "ignored_columns": list(batch.ignored_columns),
        "actor": batch.actor,
        "created_at": batch.created_at,
    }


def _json_safe(view: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in view.items()}


def _page(request: Request, db: Session, template: str, ctx: dict[str, Any]) -> HTMLResponse:
    flash = pop_flash(request)
    response = render_page(request, db, template, {**ctx, "flash": flash})
    if flash:
        clear_flash(response)
    return response


@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request, pasted: str = "", outcome: str = "", db: Session = Depends(get_db)) -> HTMLResponse:
    result = None
    if pasted.isdigit():
        try:
            result = {"card": inbox_service.get_card(db, int(pasted)), "duplicate": outcome == "duplicate"}
        except DomainError:
            result = None  # the enquiry was removed meanwhile: show the page without the panel
    rows = importer.list_batches(db, limit=RECENT_BATCHES)
    return _page(
        request, db, "importer/page.html",
        {"kinds": KINDS, "kind_labels": KIND_LABELS, "batches": [batch_view(b) for b in rows], "form": {}, "result": result},
    )  # fmt: skip


@router.get("/import/batches/{batch_id}", response_class=HTMLResponse)
def batch_page(request: Request, batch_id: PathId, db: Session = Depends(get_db)) -> Response:
    view = batch_view(importer.get_batch(db, batch_id))  # not_found 404 for an unknown id
    if wants_json(request):
        return envelope_ok(_json_safe(view))
    return _page(request, db, "importer/batch.html", {"b": view, "kind_labels": KIND_LABELS})


@router.post("/import/csv", response_model=None, dependencies=[Depends(require_admin)])
def import_csv_route(
    request: Request,
    kind: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    data = file.file.read(MAX_BYTES + 1)  # bounded read; the service rejects > 5 MB
    filename = (file.filename or "upload.csv")[:255]

    def run() -> Response:
        # Empty / oversized / non-CSV input and unknown kinds are rejected by the service (import_invalid / import_too_large).
        batch = importer.import_csv(db, filename=filename, kind=kind, data=data, actor=actor_of(member))
        view = batch_view(batch)
        if wants_json(request):
            return envelope_ok(_json_safe(view), 201)
        url = f"/import/batches/{view['id']}"
        message = f"Imported {filename}: {view['new']} new, {view['merged']} merged, {view['unchanged']} unchanged."
        return crm_done(request, url, message)

    return crm_guard(request, "/import", run, db)


@router.post("/import/paste", response_model=None, dependencies=[Depends(require_admin)])
def import_paste(
    request: Request,
    text: str = Form(""),
    link: str = Form(""),
    author: str = Form(""),
    author_profile_url: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
    llm: LLMClient = Depends(get_llm),
) -> Response:
    def run() -> Response:
        if len(text) > MAX_PASTE_CHARS:
            raise DomainError(
                "import_too_large", f"That post is too long (max {MAX_PASTE_CHARS:,} characters) - paste the relevant part.", 413
            )
        for label, value, limit in (("Link", link, 2000), ("Author", author, 300), ("Author profile link", author_profile_url, 2000)):
            if len(value.strip()) > limit:
                raise DomainError("import_invalid", f"{label} is too long (max {limit} characters).", 400)
        try:
            result = importer.paste_post(
                db, llm, text=text.strip(), link=opt_str(link), author=opt_str(author),
                author_profile_url=opt_str(author_profile_url), actor=actor_of(member),
            )  # fmt: skip
        except (LLMAuthError, LLMModelNotFound) as exc:
            db.commit()  # keep the error llm_calls row
            raise DomainError(
                "llm_unavailable",
                f"The AI service refused the request ({type(exc).__name__}) - check the API key and model in .env.",
                502,
            ) from exc
        except LLMError as exc:
            raise DomainError("triage_failed", "The AI service is unavailable right now. Try again in a minute.", 502) from exc

        duplicate = result.outcome == "duplicate"
        if wants_json(request):
            return envelope_ok({"enquiry_id": result.enquiry_id, "outcome": result.outcome}, 200 if duplicate else 201)
        # The inbox page has no flash area, so the result is shown on this page (#paste-result) with a link.
        url = f"/import?pasted={result.enquiry_id}&outcome={'duplicate' if duplicate else 'created'}"
        return crm_done(request, url, "Already in your inbox." if duplicate else "Post added and triaged.")

    return crm_guard(request, "/import", run, db)



__all__ = ["batch_view", "router"]
