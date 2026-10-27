"""Review inbox (slice H): tabs, search, cards, accept/discard/override, bulk, retry."""

from __future__ import annotations

import math
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from sales0.db.engine import get_db
from sales0.errors import DomainError
from sales0.services import inbox as inbox_service
from sales0.services import offerings as offerings_service
from sales0.services import runs as runs_service
from sales0.services.views import EnquiryCardView, InboxPage
from sales0.web.context import cost_badge_oob, is_htmx, redirect, render_html, render_page, toast_html

router = APIRouter()

Tab = Literal["review", "accepted", "discarded"]
TABS: tuple[tuple[str, str], ...] = (("review", "To review"), ("accepted", "Accepted"), ("discarded", "Discarded"))
PAGE_SIZE = 25

# Preset discard reasons (ui.md). "" = "Other — use AI reason" (the service falls back to it).
DISCARD_REASONS: tuple[str, ...] = (
    "Looking for individual trainers",
    "Training agency / marketplace",
    "Too specific / not our offering",
    "Not a requirement",
    "No response",
)
PRIORITIES: tuple[tuple[str, str], ...] = (("high", "High"), ("medium", "Medium"), ("low", "Low"))

DECISION_INVALID = "Choose Accept or Discard for at least one enquiry."
_URN_RE = re.compile(r"urn:li:(?:activity|ugcPost|share):\d+")


def _tab(value: str | None) -> Tab:
    return value if value in {"review", "accepted", "discarded"} else "review"  # type: ignore[return-value]


def _page_number(value: str | int | None) -> int:
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def _q(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def first_urn(card: EnquiryCardView) -> str:
    """The first source ref (ui.md data-urn); falls back to parsing the permalink."""
    source_ref = getattr(card, "source_ref", None)
    if source_ref:
        return str(source_ref)
    match = _URN_RE.search(card.source_url or "")
    return match.group(0) if match else ""


def _load_page(db: Session, tab: Tab, q: str | None, page: int) -> InboxPage:
    result = inbox_service.list_enquiries(db, tab, q, page=page, page_size=PAGE_SIZE)
    if not result.items and page > 1 and result.total:
        # The page emptied (e.g. after deciding its last cards): show the last real page.
        last = max(1, math.ceil(result.total / result.page_size))
        result = inbox_service.list_enquiries(db, tab, q, page=last, page_size=PAGE_SIZE)
    return result


def _list_ctx(db: Session, inbox: InboxPage) -> dict:
    pages = max(1, math.ceil(inbox.total / inbox.page_size)) if inbox.page_size else 1
    has_runs = bool(runs_service.list_runs(db, limit=1)) if inbox.tab == "review" and not inbox.total and not inbox.q else True
    return {
        "inbox": inbox,
        "tab": inbox.tab,
        "q": inbox.q or "",
        "page": inbox.page,
        "pages": pages,
        "tabs": TABS,
        "has_runs": has_runs,
        "offerings": offerings_service.list_offerings(db),
        "discard_reasons": DISCARD_REASONS,
        "priorities": PRIORITIES,
        "first_urn": first_urn,
    }


def _card_html(request: Request, db: Session, card: EnquiryCardView, tab: str, q: str, page: int) -> str:
    return render_html(
        request,
        "inbox/_card.html",
        {
            "card": card,
            "tab": _tab(tab),
            "q": q,
            "page": page,
            "offerings": offerings_service.list_offerings(db),
            "discard_reasons": DISCARD_REASONS,
            "priorities": PRIORITIES,
            "first_urn": first_urn,
        },
    )


# ---------------------------------------------------------------------- page
@router.get("/inbox", response_class=HTMLResponse)
def inbox_page(
    request: Request, tab: str | None = None, q: str | None = None, page: str | None = None, db: Session = Depends(get_db)
) -> HTMLResponse:
    inbox = _load_page(db, _tab(tab), _q(q), _page_number(page))
    return render_page(request, db, "inbox/page.html", _list_ctx(db, inbox))


# ---------------------------------------------------------------------- actions
@router.post("/inbox/decide", response_model=None)
def decide(
    request: Request,
    enquiry_ids: list[str] = Form(default_factory=list),
    decision: str = Form(""),
    discard_reason: str = Form(""),
    tab: str = Form("review"),
    q: str = Form(""),
    page: str = Form("1"),
    db: Session = Depends(get_db),
) -> Response:
    ids: list[int] = []
    for raw in enquiry_ids:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            raise DomainError("not_found", "That item no longer exists — refresh the page.", 404) from None
    ids = list(dict.fromkeys(ids))
    if decision not in {"accept", "discard"} or not ids:
        raise DomainError("decision_invalid", DECISION_INVALID, 400)
    reason = (discard_reason.strip() or None) if decision == "discard" else None
    decided = inbox_service.decide(db, ids, decision, discard_reason=reason)  # type: ignore[arg-type]
    current_tab, query, page_no = _tab(tab), _q(q), _page_number(page)
    if not is_htmx(request):
        return redirect("/inbox", tab=current_tab, q=query, page=page_no if page_no > 1 else None)
    inbox = _load_page(db, current_tab, query, page_no)
    ctx = _list_ctx(db, inbox)
    verb = "Accepted" if decision == "accept" else "Discarded"
    body = (
        render_html(request, "inbox/_list.html", ctx)
        + render_html(request, "inbox/_tabs.html", {**ctx, "oob": True})
        + toast_html(f"{verb} {len(decided) or len(ids)}")
    )
    return HTMLResponse(body)


@router.post("/inbox/{enquiry_id}/fields", response_model=None)
def update_fields(
    request: Request,
    enquiry_id: int,
    priority: str | None = Form(None),
    offering_id: str | None = Form(None),
    tab: str = Form("review"),
    q: str = Form(""),
    page: str = Form("1"),
    db: Session = Depends(get_db),
) -> Response:
    # Each select posts only its own field. An absent offering_id keeps the offering
    # (the service's UNSET default); "none"/"" means "No match" (offering_id=None).
    kwargs: dict[str, Any] = {}
    if priority not in (None, ""):
        kwargs["priority"] = priority
    if offering_id is not None:
        if offering_id.strip() in ("", "none"):
            kwargs["offering_id"] = None
        else:
            try:
                kwargs["offering_id"] = int(offering_id)
            except ValueError:
                raise DomainError("not_found", "That item no longer exists — refresh the page.", 404) from None
    if not kwargs:
        raise DomainError("field_invalid", "Choose a priority or an offering to change.", 400)
    card = inbox_service.update_fields(db, enquiry_id, **kwargs)
    if not is_htmx(request):
        return redirect("/inbox", tab=_tab(tab), q=_q(q))
    message = "Priority updated." if "priority" in kwargs else "Offering updated."
    return HTMLResponse(_card_html(request, db, card, tab, q, _page_number(page)) + toast_html(message))


@router.post("/inbox/{enquiry_id}/retry", response_model=None)
def retry(
    request: Request,
    enquiry_id: int,
    tab: str = Form("review"),
    q: str = Form(""),
    page: str = Form("1"),
    db: Session = Depends(get_db),
) -> Response:
    try:
        card = inbox_service.retry_triage(db, enquiry_id)
        toast = toast_html("AI triage finished.")
    except DomainError as exc:
        if exc.code == "not_found":
            raise
        # The card stays failed with the new error; the toast says what happened.
        card = inbox_service.get_card(db, enquiry_id)
        toast = toast_html(exc.message, "error")
    if not is_htmx(request):
        return redirect("/inbox", tab=_tab(tab), q=_q(q))
    return HTMLResponse(_card_html(request, db, card, tab, q, _page_number(page)) + cost_badge_oob(db) + toast)


__all__ = ["router", "DISCARD_REASONS", "first_urn"]
