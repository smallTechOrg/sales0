"""Shell routes (slice E): ``/`` → ``/inbox``, header fragments, and the labelled
stub page for the one feature that was deferred by decision (WhatsApp ingest)."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from sales0.db.engine import get_db
from sales0.services import session as session_service
from sales0.web.context import banner_text, hosted_worker, render_fragment, render_page, status_light_view

router = APIRouter()


@dataclass(frozen=True)
class StubPage:
    title: str
    phase: int
    description: str


STUBS: dict[str, StubPage] = {
    "/whatsapp": StubPage(
        "WhatsApp groups", 5,
        "Ingesting requirement messages from the team's WhatsApp groups into the review inbox. Not built: WhatsApp automation was deferred by decision.",
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
    light = status_light_view(session, hosted_worker(db))
    return render_fragment(request, "_shell/status_light.html", {"session": session, "light": light})


@router.get("/_shell/banner", response_class=HTMLResponse)
def banner(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    text = banner_text(session_service.session_status(db), hosted_worker(db))
    if not text:
        return HTMLResponse("")
    return render_fragment(request, "_shell/banner.html", {"banner": text})
