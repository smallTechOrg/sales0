"""Review inbox (slice H): tabs, search, cards, accept/discard/override, bulk, retry."""

from __future__ import annotations

import math
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from sales0.db.engine import get_db
from sales0.db.models import TeamMember
from sales0.errors import DomainError
from sales0.services import criteria as criteria_service
from sales0.services import inbox as inbox_service
from sales0.services import offerings as offerings_service
from sales0.services import runs as runs_service
from sales0.services import seen_posts as seen_posts_service
from sales0.services.views import EnquiryCardView, InboxPage, SkippedPage
from sales0.web.acting import acted_by_id, get_acting_member_optional, require_admin
from sales0.web.context import PathId
from sales0.web.context import cost_badge_oob, is_htmx, redirect, render_html, render_page, toast_html

router = APIRouter()

Tab = Literal["review", "accepted", "discarded", "skipped"]
TABS: tuple[tuple[str, str], ...] = (
    ("review", "To review"),
    ("accepted", "Accepted"),
    ("discarded", "Discarded"),
    ("skipped", "Skipped by AI"),
)
SkippedState = Literal["discard", "failed"]
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
    return value if value in {"review", "accepted", "discarded", "skipped"} else "review"  # type: ignore[return-value]


def _state(value: str | None) -> SkippedState:
    return "failed" if value == "failed" else "discard"


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
    if tab == "skipped":  # cards are never decided from the Skipped tab
        tab = "review"
    result = inbox_service.list_enquiries(db, tab, q, page=page, page_size=PAGE_SIZE)
    if not result.items and page > 1 and result.total:
        # The page emptied (e.g. after deciding its last cards): show the last real page.
        last = max(1, math.ceil(result.total / result.page_size))
        result = inbox_service.list_enquiries(db, tab, q, page=last, page_size=PAGE_SIZE)
    return result


def _tab_counts(db: Session) -> dict[str, int]:
    """The four tab counts (+ ``skipped_failed``) from the inbox service."""
    return dict(inbox_service.list_enquiries(db, "review", None, page=1, page_size=1).counts)


def _skipped_ctx(db: Session, q: str | None, state: SkippedState, page: int) -> dict:
    result: SkippedPage = seen_posts_service.list_skipped(db, q=q, state=state, page=page, page_size=PAGE_SIZE)
    if not result.items and page > 1 and result.total:
        last = max(1, math.ceil(result.total / result.page_size))
        result = seen_posts_service.list_skipped(db, q=q, state=state, page=last, page_size=PAGE_SIZE)
    counts = _tab_counts(db)
    counts["skipped"] = result.counts.get("skipped", result.counts.get("discard", counts.get("skipped", 0)))
    counts["skipped_failed"] = result.counts.get("skipped_failed", result.counts.get("failed", counts.get("skipped_failed", 0)))
    pages = max(1, math.ceil(result.total / result.page_size)) if result.page_size else 1
    return {
        "skipped": result,
        "counts": counts,
        "tab": "skipped",
        "state": state,
        "q": q or "",
        "page": result.page,
        "pages": pages,
        "tabs": TABS,
    }


def _skipped_response(request: Request, db: Session, q: str, state: str, page: str, toast: str) -> HTMLResponse:
    ctx = _skipped_ctx(db, _q(q), _state(state), _page_number(page))
    body = (
        render_html(request, "inbox/_skipped.html", ctx)
        + render_html(request, "inbox/_tabs.html", {**ctx, "oob": True})
        + cost_badge_oob(db)
        + toast
    )
    return HTMLResponse(body)


def _is_admin(request: Request, db: Session) -> bool:
    """The real user may edit triage criteria (admins; open mode: the acting admin) - hides "save as rule" otherwise."""
    try:
        require_admin(request, db)
    except DomainError:
        return False
    return True


MAX_CUSTOM_REASON = 200


def _discard_reason(preset: str, custom: str) -> str | None:
    """The team's own words win over the preset; both empty = the AI's reason (service fallback)."""
    text = " ".join((custom or "").split())
    if len(text) > MAX_CUSTOM_REASON:
        raise DomainError("decision_invalid", f"Keep the reason under {MAX_CUSTOM_REASON} characters.", 400)
    return text or (preset.strip() or None)


def _list_ctx(db: Session, inbox: InboxPage, request: Request | None = None) -> dict:
    pages = max(1, math.ceil(inbox.total / inbox.page_size)) if inbox.page_size else 1
    has_runs = bool(runs_service.list_runs(db, limit=1)) if inbox.tab == "review" and not inbox.total and not inbox.q else True
    return {
        "inbox": inbox,
        "counts": inbox.counts,
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
        "can_save_rule": _is_admin(request, db) if request is not None else False,
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
            "can_save_rule": _is_admin(request, db),
        },
    )


# ---------------------------------------------------------------------- page
@router.get("/inbox", response_class=HTMLResponse)
def inbox_page(
    request: Request,
    tab: str | None = None,
    state: str | None = None,
    q: str | None = None,
    page: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if _tab(tab) == "skipped":
        ctx = _skipped_ctx(db, _q(q), _state(state), _page_number(page))
        if is_htmx(request) and request.headers.get("HX-Target") == "inbox-list":
            return HTMLResponse(render_html(request, "inbox/_skipped.html", ctx))
        return render_page(request, db, "inbox/page.html", ctx)
    inbox = _load_page(db, _tab(tab), _q(q), _page_number(page))
    return render_page(request, db, "inbox/page.html", _list_ctx(db, inbox, request))


# ---------------------------------------------------------------------- actions
@router.post("/inbox/decide", response_model=None)
def decide(
    request: Request,
    enquiry_ids: list[str] = Form(default_factory=list),
    decision: str = Form(""),
    discard_reason: str = Form(""),
    custom_reason: str = Form(""),
    save_rule: str = Form(""),
    tab: str = Form("review"),
    q: str = Form(""),
    page: str = Form("1"),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
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
    reason = _discard_reason(discard_reason, custom_reason) if decision == "discard" else None
    wants_rule = decision == "discard" and save_rule.strip().lower() in ("1", "true", "on", "yes")
    if wants_rule:
        require_admin(request, db)  # 403 for members (the checkbox is only shown to admins)
        if not reason:
            raise DomainError("decision_invalid", "Write or pick a reason to save it as a triage rule.", 400)
    decided = inbox_service.decide(
        db, ids, decision, discard_reason=reason, actor_member_id=member.id if member else None,
        acted_by_member_id=acted_by_id(member),
    )  # type: ignore[arg-type]
    rule_note = ""
    if wants_rule and reason:
        rule = criteria_service.rule_from_discard(
            db, reason, enquiry_id=ids[0], actor_member_id=member.id if member else None
        )
        rule_note = " · saved as a triage rule" if rule is not None else " · that rule already exists"
    current_tab, query, page_no = _tab(tab), _q(q), _page_number(page)
    if not is_htmx(request):
        return redirect("/inbox", tab=current_tab, q=query, page=page_no if page_no > 1 else None)
    inbox = _load_page(db, current_tab, query, page_no)
    ctx = _list_ctx(db, inbox, request)
    verb = "Accepted" if decision == "accept" else "Discarded"
    body = (
        render_html(request, "inbox/_list.html", ctx)
        + render_html(request, "inbox/_tabs.html", {**ctx, "oob": True})
        + toast_html(f"{verb} {len(decided) or len(ids)}{rule_note}")
    )
    return HTMLResponse(body)


# ---------------------------------------------------------------------- Skipped by AI (Phase 1.4)
def _skipped_action(
    request: Request, db: Session, action: str, seen_post_id: PathId, q: str, state: str, page: str,
    member: TeamMember | None = None,
) -> Response:
    toast = ""
    try:
        if action == "rescue":
            seen_posts_service.rescue(
                db, seen_post_id, accept=False, actor_user_id=member.id if member else None,
                acted_by_member_id=acted_by_id(member),
            )
            toast = toast_html("Moved to To review")
        elif action == "override":
            seen_posts_service.rescue(
                db, seen_post_id, accept=True, actor_user_id=member.id if member else None,
                acted_by_member_id=acted_by_id(member),
            )
            toast = toast_html("Accepted as Prospect · New (overrode AI)")
        else:
            seen_posts_service.retry_triage(db, seen_post_id)
            toast = toast_html("AI triage finished.")
    except DomainError as exc:
        if exc.code == "not_found":
            raise
        # raw_purged / already_rescued / triage_failed: the list is re-shown with the reason.
        toast = toast_html(exc.message, "error")
    if not is_htmx(request):
        return redirect("/inbox", tab="skipped", state=state if state == "failed" else None, q=_q(q))
    return _skipped_response(request, db, q, state, page, toast)


@router.post("/inbox/skipped/{seen_post_id}/rescue", response_model=None)
def skipped_rescue(
    request: Request,
    seen_post_id: PathId,
    q: str = Form(""),
    state: str = Form("discard"),
    page: str = Form("1"),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    return _skipped_action(request, db, "rescue", seen_post_id, q, state, page, member)


@router.post("/inbox/skipped/{seen_post_id}/override", response_model=None)
def skipped_override(
    request: Request,
    seen_post_id: PathId,
    q: str = Form(""),
    state: str = Form("discard"),
    page: str = Form("1"),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    return _skipped_action(request, db, "override", seen_post_id, q, state, page, member)


@router.post("/inbox/skipped/{seen_post_id}/retry", response_model=None)
def skipped_retry(
    request: Request,
    seen_post_id: PathId,
    q: str = Form(""),
    state: str = Form("failed"),
    page: str = Form("1"),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    return _skipped_action(request, db, "retry", seen_post_id, q, state, page, member)


@router.post("/inbox/{enquiry_id}/fields", response_model=None)
def update_fields(
    request: Request,
    enquiry_id: PathId,
    priority: str | None = Form(None),
    offering_id: str | None = Form(None),
    tab: str = Form("review"),
    q: str = Form(""),
    page: str = Form("1"),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
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
    card = inbox_service.update_fields(
        db, enquiry_id, actor_user_id=member.id if member else None, acted_by_member_id=acted_by_id(member), **kwargs
    )
    if not is_htmx(request):
        return redirect("/inbox", tab=_tab(tab), q=_q(q))
    message = "Priority updated." if "priority" in kwargs else "Offering updated."
    return HTMLResponse(_card_html(request, db, card, tab, q, _page_number(page)) + toast_html(message))


@router.post("/inbox/{enquiry_id}/retry", response_model=None)
def retry(
    request: Request,
    enquiry_id: PathId,
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
