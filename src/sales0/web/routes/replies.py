"""Reply tracking pages: the replies list, manual "Check now", and the suppression list.

Replies are untrusted third-party text: templates render the snippet autoescaped only.
A DomainError from a service is an error flash for plain form posts and the JSON/htmx
envelope otherwise (same convention as the outreach pages).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.engine import get_db
from sales0.db.models import OutreachReply, TeamMember
from sales0.errors import DomainError
from sales0.jobs.reply_poller import get_poller
from sales0.services import replies as replies_service
from sales0.services import suppressions as suppressions_service
from sales0.web.acting import clear_flash, get_acting_member, pop_flash, set_flash
from sales0.web.context import PathId
from sales0.web.context import is_htmx, render_fragment, render_page
from sales0.web.envelope import wants_json

router = APIRouter()

KIND_FILTERS = (("", "All kinds"), ("human", "Human"), ("auto_reply", "Auto-reply"), ("bounce", "Bounce"))
KIND_LABELS = {"human": "Human", "auto_reply": "Auto-reply", "bounce": "Bounce"}
INTENT_FILTERS = (
    ("", "Any intent"), ("interested", "Interested"), ("not_now", "Not now"), ("unsubscribe", "Unsubscribe"),
    ("question", "Question"), ("other", "Other"),
)
INTENT_LABELS = {k: v for k, v in INTENT_FILTERS if k}
HANDLED_FILTERS = (("open", "Open"), ("handled", "Handled"), ("all", "All"))
REASON_LABELS = {"unsubscribe": "Unsubscribed", "bounce": "Hard bounce", "manual": "Added manually"}


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


def _page(request: Request, db: Session, template: str, ctx: dict[str, Any]) -> HTMLResponse:
    flash = pop_flash(request)
    response = render_page(request, db, template, {**ctx, "flash": flash})
    if flash:
        clear_flash(response)
    return response


def _item(view: replies_service.ReplyView, show_owner: bool) -> dict[str, Any]:
    return {"r": view.reply, "msg": view.message, "owner": view.owner, "show_owner": show_owner}


# ---------------------------------------------------------------------- list
@router.get("/replies", response_class=HTMLResponse)
def replies_page(
    request: Request,
    kind: str | None = None,
    intent: str | None = None,
    handled: str | None = None,
    scope: str | None = None,
    message: int | None = None,
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
) -> HTMLResponse:
    kind = kind if kind in KIND_LABELS else ""
    intent = intent if intent in INTENT_LABELS else ""
    handled = handled if handled in ("open", "handled", "all") else "open"
    everyone = scope == "all" and member.role == "admin"
    views = replies_service.list_replies(
        db, member, kind=kind or None, intent=intent or None,
        handled={"open": False, "handled": True, "all": None}[handled], everyone=everyone,
    )
    if message is not None:
        views = [v for v in views if v.message.id == message]
    items = [_item(v, everyone) for v in views]
    return _page(
        request, db, "replies/list.html",
        {
            "items": items, "kind": kind, "intent": intent, "handled": handled, "scope": "all" if everyone else "mine",
            "message_filter": message, "kind_filters": KIND_FILTERS, "intent_filters": INTENT_FILTERS,
            "handled_filters": HANDLED_FILTERS, "kind_labels": KIND_LABELS, "intent_labels": INTENT_LABELS,
            "is_admin": member.role == "admin", "section": "replies", "open_ids": [i["r"].id for i in items if not i["r"].handled],
            "health": replies_service.poller_health(db),
        },
    )


@router.post("/replies/{reply_id}/handled", response_model=None)
def set_reply_handled(
    request: Request,
    reply_id: PathId,
    handled: str = Form("1"),
    back: str = Form("/replies"),
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
) -> Response:
    flag = handled not in ("0", "", "false")
    if is_htmx(request) and not wants_json(request):
        row = replies_service.set_handled(db, reply_id, member, flag)
        owner = db.execute(select(TeamMember.name).where(TeamMember.id == row.member_id)).scalar_one_or_none() or "?"
        from sales0.db.models import OutreachMessage

        msg = db.get(OutreachMessage, row.message_id)
        item = {"r": row, "msg": msg, "owner": owner, "show_owner": member.role == "admin" and row.member_id != member.id}
        return render_fragment(request, "replies/_row.html", {"it": item, "kind_labels": KIND_LABELS, "intent_labels": INTENT_LABELS})

    def act() -> str:
        replies_service.set_handled(db, reply_id, member, flag)
        return "Marked as handled." if flag else "Reopened."

    target = back if back.startswith("/replies") else "/replies"
    return _guard(request, target, act)


@router.post("/replies/handled-all", response_model=None)
def mark_shown_handled(
    request: Request,
    reply_id: list[int] = Form(default_factory=list),
    back: str = Form("/replies"),
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
) -> Response:
    def act() -> str:
        n = replies_service.mark_all_handled(db, member, reply_id)
        return f"Marked {n} repl{'y' if n == 1 else 'ies'} as handled."

    return _guard(request, back if back.startswith("/replies") else "/replies", act)


@router.post("/replies/check", response_model=None)
def check_now(request: Request, member: TeamMember = Depends(get_acting_member)) -> Response:
    def act() -> str:
        results = get_poller().run_cycle(force=True)
        if results is None:
            return "A check is already running - try again in a moment."
        threads = sum(r.threads_checked for r in results)
        new = sum(r.new_replies for r in results)
        return f"Checked {threads} conversation{'s' if threads != 1 else ''}, {new} new repl{'y' if new == 1 else 'ies'}."

    return _guard(request, "/replies", act)


# ---------------------------------------------------------------------- suppressions
@router.get("/replies/suppressions", response_class=HTMLResponse)
def suppressions_page(
    request: Request, db: Session = Depends(get_db), member: TeamMember = Depends(get_acting_member)
) -> HTMLResponse:
    rows = suppressions_service.list_all(db)
    source_ids = [r.source_reply_id for r in rows if r.source_reply_id]
    sources: dict[int, OutreachReply] = {}
    if source_ids:
        sources = {r.id: r for r in db.execute(select(OutreachReply).where(OutreachReply.id.in_(source_ids))).scalars()}
    items = []
    for row in rows:
        src = sources.get(row.source_reply_id or 0)
        link = f"/outreach/messages/{src.message_id}" if src and (member.role == "admin" or src.member_id == member.id) else None
        items.append({"s": row, "reason_label": REASON_LABELS.get(row.reason, row.reason), "link": link, "has_source": src is not None})
    return _page(
        request, db, "replies/suppressions.html",
        {"items": items, "is_admin": member.role == "admin", "section": "suppressions"},
    )


@router.post("/replies/suppressions", response_model=None)
def add_suppression(
    request: Request,
    address: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
) -> Response:
    def act() -> str:
        row = suppressions_service.add(db, address, "manual", note=note.strip() or None)
        return f"{row.address} is on the suppression list. sales0 will not draft or send to it."

    return _guard(request, "/replies/suppressions", act)


@router.post("/replies/suppressions/{suppression_id}/delete", response_model=None)
def remove_suppression(
    request: Request, suppression_id: PathId, db: Session = Depends(get_db), member: TeamMember = Depends(get_acting_member)
) -> Response:
    def act() -> str:
        if member.role != "admin":
            raise DomainError("forbidden", "Only an admin can remove an address from the suppression list.", 403)
        suppressions_service.remove(db, suppression_id)
        return "Removed from the suppression list."

    return _guard(request, "/replies/suppressions", act)
