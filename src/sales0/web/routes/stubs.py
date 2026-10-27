"""Shell routes (slice E): ``/`` → ``/inbox``, header fragments, and the labelled
"Coming in Phase N" stub pages for features that later phases build."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from sales0.db.engine import get_db
from sales0.services import session as session_service
from sales0.web.context import banner_text, render_fragment, render_page, status_light_view

router = APIRouter()


@dataclass(frozen=True)
class StubPage:
    title: str
    phase: int
    description: str


STUBS: dict[str, StubPage] = {
    "/pipeline": StubPage(
        "Pipeline", 2,
        "Accepted enquiries become Prospects, then Leads and Clients, with stages, follow-ups due today and a full timeline.",
    ),
    "/contacts": StubPage(
        "Contacts", 2,
        "One shared address book of every person and company, linked to their enquiries, prospects and leads.",
    ),
    "/competitors": StubPage(
        "Competitors & Groups", 2,
        "Registries of competitor training firms and the WhatsApp/LinkedIn groups the team watches, with competitor-mention flags.",
    ),
    "/import": StubPage(
        "Import", 2,
        "Import the existing Active and Discarded Enquiries sheets (CSV) and paste single posts by hand, with duplicates merged.",
    ),
    "/outreach": StubPage(
        "Outreach", 3,
        "Research a company, draft a personalised email with the right deck, and send it from Gmail only after you approve that exact message.",
    ),
    "/metrics": StubPage(
        "Metrics", 4,
        "The Weekly and Secondary metrics sheets, produced automatically per ISO week against your targets, plus the AI cost view.",
    ),
    "/whatsapp": StubPage(
        "WhatsApp groups", 5,
        "Ingest requirement messages from the team's WhatsApp groups into the same review inbox.",
    ),
}


@router.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/inbox", status_code=303)


def _make_stub_route(path: str, stub: StubPage):  # noqa: ANN202 — FastAPI endpoint factory
    def stub_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
        return render_page(request, db, "stubs/stub.html", {"stub": stub})

    stub_page.__name__ = f"stub_{path.strip('/')}"
    return stub_page


for _path, _stub in STUBS.items():
    router.add_api_route(_path, _make_stub_route(_path, _stub), methods=["GET"], response_class=HTMLResponse)


@router.get("/_shell/status-light", response_class=HTMLResponse)
def status_light(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    session = session_service.session_status(db)
    return render_fragment(request, "_shell/status_light.html", {"session": session, "light": status_light_view(session)})


@router.get("/_shell/banner", response_class=HTMLResponse)
def banner(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    text = banner_text(session_service.session_status(db))
    if not text:
        return HTMLResponse("")
    return render_fragment(request, "_shell/banner.html", {"banner": text})
