"""Outreach pages (Phase 3): outbox, compose (+ research), message review/approve/send,
templates and decks.

Nothing here sends mail on its own: the only call that can is ``messages.send`` /
``messages.retry_send``, reached from the explicit Send / Retry POSTs on the message page.

Error handling: a ``DomainError`` from a service is re-raised for htmx and JSON callers (the
envelope turns it into a toast / JSON error with its code) and shown as a red flash after a
redirect for plain browser form posts, so the user never lands on a bare error page.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from sales0.agent.research import SafeFetcher
from sales0.clock import ensure_utc, utcnow
from sales0.config import get_settings
from sales0.db.engine import get_db
from sales0.db.models import CompanyBrief, Deck, Enquiry, Offering, OutreachMessage, TeamMember
from sales0.errors import DomainError
from sales0.llm import LLMAuthError, LLMClient, LLMError, LLMModelNotFound, get_llm_client
from sales0.logging import get_logger
from sales0.services import followup_drafts as followup_drafts_service
from sales0.services import offerings as offerings_service
from sales0.services import replies as replies_service
from sales0.services import suppressions as suppressions_service
from sales0.services.outreach import decks as decks_service
from sales0.services.outreach import drafting, messages, templates
from sales0.services.outreach._shared import split_acted_by
from sales0.services.outreach import recipients as recipients_service
from sales0.services.outreach import research as research_service
from sales0.services.team import connections as connections_service
from sales0.services.team import members as members_service
from sales0.web.acting import clear_flash, get_acting_member, pop_flash, require_admin, set_flash
from sales0.web.context import PathId
from sales0.web.context import cost_badge_oob, is_htmx, render_html, render_page, toast_html
from sales0.web.envelope import wants_json

log = get_logger("sales0.outreach.web")
router = APIRouter()

STATE_LABELS: dict[str, str] = {
    "draft": "Draft",
    "edited": "Edited",
    "approved": "Approved",
    "sending": "Sending",
    "sent": "Sent",
    "failed": "Failed",
    "rejected": "Rejected",
}
STATE_FILTERS: tuple[tuple[str, str], ...] = (("", "All states"), *STATE_LABELS.items())
EVENT_LABELS: dict[str, str] = {
    "drafted": "Draft created",
    "edited": "Edited",
    "approval_reset": "Approval reset",
    "approved": "Approved",
    "rejected": "Rejected",
    "sending": "Sending started",
    "sent": "Sent",
    "send_failed": "Send failed",
    "send_blocked": "Send blocked",
    "retry_confirmed": "Retry confirmed",
}
STUCK_AFTER = timedelta(minutes=5)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

SAMPLE_VALUES = {
    "first_name": "Priya",
    "company": "Acme Logistics",
    "requirement": "a GenAI workshop for 40 managers",
}


def get_llm() -> LLMClient:
    """The LLM client for outreach routes: the same process-wide seam triage uses
    (``sales0.llm.set_llm_client`` in tests); override with ``app.dependency_overrides``."""
    return get_llm_client()


# ---------------------------------------------------------------------- small helpers
def _opt_int(value: str | int | None) -> int | None:
    if value in (None, "", "none"):
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise DomainError("bad_request", "That selection is not valid - refresh the page.", 400) from None
    if not 0 <= number <= 2**31 - 1:
        raise DomainError("bad_request", "That selection is not valid - refresh the page.", 400)
    return number


def _flash_redirect(path: str, message: str, kind: str = "ok") -> RedirectResponse:
    response = RedirectResponse(path, status_code=303)
    set_flash(response, message, kind)
    return response


def _guard(request: Request, back: str, action: Callable[[], str]) -> Response:
    """Run ``action`` (returns the success flash). Domain errors become an error flash for
    browser form posts; htmx and JSON callers get the envelope instead."""
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


def _cap_info(db: Session, member: TeamMember) -> dict[str, Any]:
    cap = get_settings().gmail_daily_send_cap
    used = messages.member_sends_today(db, member.id)
    return {"used": used, "cap": cap, "reached": used >= cap}


def _member_names(db: Session) -> dict[int, str]:
    return {m.id: m.name for m in members_service.list_members(db)}


def _normalise_website(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise DomainError("website_invalid", "Enter the website as a normal address like https://acme.example.", 400)
    return value


def _json_dict(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _safe_links(urls: list[Any]) -> list[str]:
    return [u for u in (str(x) for x in urls) if u.startswith(("http://", "https://"))]


def brief_view(row: CompanyBrief) -> dict[str, Any]:
    data = _json_dict(row.brief_json) or {}
    sources = _safe_links(data.get("sources") or json.loads(row.sources_json or "[]"))
    return {
        "id": row.id,
        "company": row.company_name,
        "status": data.get("research_status") or "done",
        "error": data.get("research_error"),
        "what_they_do": data.get("what_they_do"),
        "size_hints": data.get("size_hints"),
        "needs": data.get("ai_training_needs") or [],
        "suggested_offering": data.get("suggested_offering"),
        "hooks": data.get("personalisation_hooks") or [],
        "sources": sources,
        "limited": bool(data.get("limited_information")),
        "cost_usd_micros": row.cost_usd_micros,
    }


def doc_brief_view(doc: Any) -> dict[str, Any]:
    """The compose brief panel for a prospect research VERSION (``services.research.docs.DocView``)."""
    data = doc.data or {}
    return {
        "id": doc.id,
        "field": "research_doc_id",
        "company": data.get("company_name") or "",
        "status": doc.status,
        "error": doc.error,
        "what_they_do": data.get("what_they_do"),
        "size_hints": data.get("size_hints"),
        "needs": data.get("ai_training_needs") or [],
        "suggested_offering": data.get("suggested_offering"),
        "hooks": data.get("personalisation_hooks") or [],
        "sources": [s.url for s in doc.sources if s.url],
        "limited": bool(data.get("limited_information")),
        "cost_usd_micros": doc.cost_usd_micros,
        "version": doc.version,
        "origin_label": doc.origin_label,
        "prospect_id": doc.prospect_id,
    }


# ---------------------------------------------------------------------- outbox
@router.get("/outreach", response_class=HTMLResponse)
def outbox(
    request: Request,
    state: str | None = None,
    scope: str | None = None,
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
) -> HTMLResponse:
    state = state if state in STATE_LABELS else ""
    everyone = scope == "all" and member.role == "admin"
    rows = messages.list_outbox(db, member, state or None, everyone=everyone)
    names = _member_names(db)
    now = utcnow()
    items = [
        {
            "msg": m,
            "owner": names.get(m.member_id, "?"),
            "stuck": m.state == "sending" and now - ensure_utc(m.updated_at) > STUCK_AFTER,
        }
        for m in rows
    ]
    return _page(
        request, db, "outreach/outbox.html",
        {
            "reply_counts": replies_service.reply_counts_by_message(db, [m.id for m in rows]),
            "items": items, "state": state, "scope": "all" if everyone else "mine", "state_filters": STATE_FILTERS,
            "state_labels": STATE_LABELS, "cap": _cap_info(db, member), "stuck_count": sum(1 for i in items if i["stuck"]),
            "is_admin": member.role == "admin", "connection": connections_service.connection_status(db, member.id),
            "section": "outbox", "due_followups": followup_drafts_service.due_overview(db, member),
            "followup_batch": get_settings().followup_draft_batch,
        },
    )  # fmt: skip


# ---------------------------------------------------------------------- compose + research
def _defaults_ctx(db: Session, offering_id: int | None, template_id: int | None = None, deck_id: int | None = None) -> dict[str, Any]:
    """Template + deck selects with the offering's defaults, and a truthful note on WHY a deck is (not) picked:
    the attachment must never silently default to "No deck"."""
    tpl = templates.get_template(db, template_id) if template_id else templates.template_for_offering(db, offering_id)
    deck = decks_service.get_deck(db, deck_id) if deck_id else decks_service.deck_for_offering(db, offering_id)
    all_decks = decks_service.list_decks(db)
    offering = db.get(Offering, offering_id) if offering_id else None
    lost = decks_service.missing_for_offering(db, offering_id)
    if deck is not None and deck.content_missing:
        deck, lost = None, deck
    if lost is not None and (deck is None or deck.offering_id != offering_id):
        deck_note = ("missing", f"The deck '{lost.name}' mapped to this offering has no file (file missing - upload it again on "
                                "Outreach > Decks)." + (f" Using {decks_service.attachment_label(deck)} instead." if deck else
                                " No deck is selected: pick one or the email goes out without an attachment."))
    elif deck is not None and offering is not None and deck.offering_id == offering.id:
        deck_note = ("ok", f"Attached by default: {decks_service.attachment_label(deck)} (mapped to {offering.name}).")
    elif deck is not None:
        why = f"no deck is mapped to {offering.name}" if offering is not None else "no offering is chosen"
        deck_note = ("warn", f"Attached by default: {decks_service.attachment_label(deck)} (a generic deck - {why}).")
    elif all_decks:
        why = f"No deck is mapped to {offering.name}" if offering is not None else "No offering is chosen"
        deck_note = ("warn", f"{why}, so no deck is selected: this email will go out WITHOUT an attachment unless you pick one.")
    else:
        deck_note = ("warn", "No decks uploaded yet: this email will go out without an attachment. Upload one on Outreach > Decks.")
    return {
        "templates": templates.list_templates(db),
        "decks": all_decks,
        "selected_template": tpl.id if tpl else None,
        "selected_deck": deck.id if deck else None,
        "deck_note": deck_note,
    }


def _prospect_prefill(db: Session, prospect_id: int) -> dict[str, Any]:
    """Compose fields from a CRM prospect: its primary contact, company and newest enquiry.

    Only what the CRM actually knows is filled in; nothing is invented for the rest.
    """
    from sales0.services.crm import prospects as prospects_service

    prospect = prospects_service.get_prospect(db, prospect_id)  # not_found 404 (envelope / error page)
    contact = next((c for c in prospect.contacts if c.id == prospect.contact_id), None) or (
        prospect.contacts[0] if prospect.contacts else None
    )
    newest = max(prospect.enquiries, key=lambda e: ensure_utc(e.date_added) if e.date_added else _EPOCH, default=None)
    return {
        "prospect_id": prospect.id,
        "company_id": prospect.company_id,
        "enquiry_id": newest.id if newest else None,
        "contact_name": (contact.full_name if contact else None) or prospect.name or "",
        "email": (contact.email if contact else None) or "",
        "company": prospect.company_name or "",
        "website": _company_website(db, prospect.company_id),
        "requirement": ((newest.requirement_summary if newest else None) or "").strip(),
    }


def _company_website(db: Session, company_id: int | None) -> str:
    from sales0.db.models import Company

    company = db.get(Company, company_id) if company_id else None
    return (company.website or "") if company is not None else ""


def _prefill(db: Session, enquiry_id: int | None, prospect_id: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "enquiry_id": None, "prospect_id": None, "company_id": None, "contact_name": "", "email": "", "company": "",
        "website": "", "requirement": "", "offering_id": None,
    }  # fmt: skip
    if prospect_id is not None:
        out.update(_prospect_prefill(db, prospect_id))
        if enquiry_id is None:
            enquiry_id = out["enquiry_id"]
    if enquiry_id is None:
        return out
    enquiry = db.get(Enquiry, enquiry_id)
    if enquiry is None:
        raise DomainError("not_found", "That enquiry no longer exists - open Compose without it.", 404)
    # Prospect-derived values win; the enquiry only fills what is still blank (never overwrites with empties).
    from_enq = {
        "contact_name": enquiry.contact_person or enquiry.author_name or "",
        "email": enquiry.email or "",
        "company": enquiry.company or "",
        "requirement": (enquiry.requirement_summary or (enquiry.raw_text or "")[:500]).strip(),
    }
    out.update(enquiry_id=enquiry.id, offering_id=enquiry.offering_id)
    for key, value in from_enq.items():
        if not out.get(key):
            out[key] = value
    return out


@router.get("/outreach/compose", response_class=HTMLResponse)
def compose(
    request: Request,
    enquiry_id: str | None = None,
    prospect_id: str | None = None,
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
) -> HTMLResponse:
    form = _prefill(db, _opt_int(enquiry_id), _opt_int(prospect_id))
    current_brief = None
    if form["prospect_id"]:
        from sales0.services.research import prospect as prospect_research

        _, doc = prospect_research.brief_for_drafting(db, form["prospect_id"])
        current_brief = doc_brief_view(doc) if doc is not None else None
    ctx = {
        "current_brief": current_brief,
        "form": form, "offerings": offerings_service.list_offerings(db), "section": "compose",
        "recipients": recipients_service.for_prospect(db, form["prospect_id"], form["company_id"]),
        "connection": connections_service.connection_status(db, member.id), "cap": _cap_info(db, member),
        **_defaults_ctx(db, form["offering_id"]),
    }  # fmt: skip
    return _page(request, db, "outreach/compose.html", ctx)


@router.get("/outreach/compose/defaults", response_class=HTMLResponse)
def compose_defaults(request: Request, offering_id: str | None = None, db: Session = Depends(get_db)) -> HTMLResponse:
    """htmx: the template + deck selects with the offering's defaults pre-selected (still editable)."""
    from sales0.web.context import render_fragment

    return render_fragment(request, "outreach/_defaults.html", _defaults_ctx(db, _opt_int(offering_id)))


@router.get("/outreach/compose/contacts", response_class=HTMLResponse)
def compose_contacts(
    request: Request, contact_q: str = "", db: Session = Depends(get_db), member: TeamMember = Depends(get_acting_member)
) -> HTMLResponse:
    """htmx: the contact picker filled with contacts matching the search (suppressed ones disabled)."""
    from sales0.web.context import render_fragment

    q = " ".join(contact_q.split())[:100]
    return render_fragment(
        request, "outreach/_recipients.html",
        {"recipients": recipients_service.search(db, q) if q else [], "selected_email": "", "searched": q},
    )  # fmt: skip


def _research_fetcher() -> SafeFetcher | None:
    hosts = get_settings().research_private_hosts  # [] unless fixture mode + explicit test switch
    return SafeFetcher(allow_private_hosts=hosts) if hosts else None


@router.post("/outreach/research", response_class=HTMLResponse)
def run_research(
    request: Request,
    company: str = Form(""),
    website: str = Form(""),
    requirement: str = Form(""),
    enquiry_id: str = Form(""),
    prospect_id: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
    llm: LLMClient = Depends(get_llm),
) -> HTMLResponse:
    company = " ".join(company.split())
    pid = _opt_int(prospect_id)
    if pid is not None:
        return _research_prospect_from_compose(request, db, member, llm, pid)
    if not company:
        raise DomainError("company_required", "Enter the company name before researching it.", 400)
    url = _normalise_website(website)
    fetcher = _research_fetcher()
    try:
        row = research_service.research_company(
            db, llm, member, company_name=company, website_url=url, context_text=requirement.strip()[:2000],
            enquiry_id=_opt_int(enquiry_id), fetcher=fetcher,
        )  # fmt: skip
    except (LLMAuthError, LLMModelNotFound) as exc:
        db.commit()  # keep the error llm_calls row
        raise DomainError("llm_unavailable", f"The AI service refused the request ({type(exc).__name__}) - check the API key and model in .env.", 502) from exc
    except LLMError as exc:
        raise DomainError("research_failed", "The AI service is unavailable right now. Try again in a minute.", 502) from exc
    finally:
        if fetcher is not None:
            fetcher.close()
    view = brief_view(row)
    body = render_html(request, "outreach/_brief.html", {"brief": view})
    if view["status"] == "failed":
        toast = toast_html("Research could not read that website. You can still draft from the requirement text.", "error")
    elif view["status"] == "partial":
        toast = toast_html("Research finished with limited information - check the brief before using it.", "ok")
    else:
        toast = toast_html("Research finished.", "ok")
    return HTMLResponse(body + cost_badge_oob(db) + toast)


def _research_prospect_from_compose(request: Request, db: Session, member: TeamMember, llm: LLMClient, prospect_id: int) -> HTMLResponse:
    """Compose's Research button for a CRM prospect: the same versioned research as the prospect page (website,
    our records, the LinkedIn page text already fetched, web search), stored as the prospect's next version. It
    does not queue a LinkedIn visit (that is the prospect page's button)."""
    from sales0.services.crm.actor import Actor
    from sales0.services.research import prospect as prospect_research

    fetcher = _research_fetcher()
    try:
        doc, _ = prospect_research.research_prospect(
            db, llm, member, prospect_id, actor=Actor.of_member(member), queue_linkedin=False, fetcher=fetcher
        )
    except (LLMAuthError, LLMModelNotFound) as exc:
        db.commit()
        raise DomainError("llm_unavailable", f"The AI service refused the request ({type(exc).__name__}) - check the API key and model in .env.", 502) from exc
    except LLMError as exc:
        raise DomainError("research_failed", "The AI service is unavailable right now. Try again in a minute.", 502) from exc
    finally:
        if fetcher is not None:
            fetcher.close()
    view = doc_brief_view(doc)
    body = render_html(request, "outreach/_brief.html", {"brief": view})
    kind, text = ("error", "Research could not find much. You can still draft from the requirement text.") if doc.status == "failed" else (
        "ok", f"Research finished - saved as version {doc.version} on the prospect.")
    return HTMLResponse(body + cost_badge_oob(db) + toast_html(text, kind))


@router.post("/outreach/draft", response_model=None)
def generate(
    request: Request,
    to: str = Form(""),
    cc: str = Form(""),
    contact_name: str = Form(""),
    company: str = Form(""),
    website: str = Form(""),
    requirement: str = Form(""),
    offering_id: str = Form(""),
    template_id: str = Form(""),
    deck_id: str = Form(""),
    brief_id: str = Form(""),
    research_doc_id: str = Form(""),
    enquiry_id: str = Form(""),
    prospect_id: str = Form(""),
    company_id: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
    llm: LLMClient = Depends(get_llm),
) -> Response:
    company = " ".join(company.split())
    if not company:
        raise DomainError("company_required", "Enter the company name - the draft is written for it.", 400)
    brief: dict[str, Any] | None = None
    brief_row = db.get(CompanyBrief, _opt_int(brief_id)) if _opt_int(brief_id) else None
    if brief_row is not None:
        brief = research_service.brief_dict(brief_row)
    account_summary: str | None = None
    pid = _opt_int(prospect_id)
    if pid is not None:
        from sales0.services.research import prospect as prospect_research
        from sales0.services.research import summary as summary_service

        if brief is None:  # the prospect's research: the version shown on Compose, else its newest usable one
            brief, _ = prospect_research.brief_for_drafting(db, pid, _opt_int(research_doc_id))
        account_summary = summary_service.summary_for_drafting(db, pid)
    no_deck = deck_id == "none"
    try:
        msg = drafting.generate_draft(
            db, llm, member, to=to, cc=cc or None, offering_id=_opt_int(offering_id), template_id=_opt_int(template_id),
            first_name=(contact_name.split() or [""])[0], company=company, requirement=requirement.strip(), brief=brief,
            deck_id=None if no_deck else _opt_int(deck_id), enquiry_id=_opt_int(enquiry_id),
            prospect_id=_opt_int(prospect_id), company_id=_opt_int(company_id), account_summary=account_summary,
        )  # fmt: skip
    except (LLMAuthError, LLMModelNotFound) as exc:
        db.commit()
        raise DomainError("llm_unavailable", f"The AI service refused the request ({type(exc).__name__}) - check the API key and model in .env.", 502) from exc
    if no_deck:
        msg.deck_id = None
        db.flush()
    url = f"/outreach/messages/{msg.id}"
    if is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return _flash_redirect(url, "Draft created - review it, then approve it.")


# ---------------------------------------------------------------------- message
def _timeline(db: Session, msg: OutreachMessage, names: dict[int, str]) -> list[tuple[Any, str, str | None, str | None]]:
    """(event, label, who, detail); an admin acting as the owner shows as "Admin as Member"."""
    out: list[tuple[Any, str, str | None, str | None]] = []
    for e in messages.list_events(db, msg.id):
        detail, by = split_acted_by(e.detail)
        who = names.get(e.member_id or 0)
        if by is not None:
            who = f"{names.get(by, '?')} as {who or '?'}"
        out.append((e, EVENT_LABELS.get(e.kind, e.kind), who, detail))
    return out


def _message_ctx(db: Session, msg: OutreachMessage, member: TeamMember) -> dict[str, Any]:
    names = _member_names(db)
    owner = msg.member_id == member.id
    owner_conn = connections_service.connection_status(db, msg.member_id)
    cap = _cap_info(db, member)
    brief = _json_dict(msg.brief_json)
    left = list(dict.fromkeys(templates.find_placeholders(msg.subject) + templates.find_placeholders(msg.body)))
    deck = db.get(Deck, msg.deck_id) if msg.deck_id else None
    hash_prefix: str | None = None
    if msg.state in ("draft", "edited") and owner:
        try:
            hash_prefix = messages._build_content(db, msg).sha256[:12]  # noqa: SLF001 - exact hash Approve will bind
        except DomainError:
            hash_prefix = None
    can_edit = owner and msg.state in messages.EDITABLE_STATES and not (msg.state == "failed" and msg.send_outcome_unknown)
    stuck = msg.state == "sending" and utcnow() - ensure_utc(msg.updated_at) > STUCK_AFTER
    return {
        "msg": msg, "owner_name": names.get(msg.member_id, "?"), "is_owner": owner,
        "state_label": STATE_LABELS.get(msg.state, msg.state),
        "events": _timeline(db, msg, names),
        "brief": ({
            "what_they_do": brief.get("what_they_do"), "hooks": brief.get("personalisation_hooks") or [],
            "needs": brief.get("ai_training_needs") or [], "sources": _safe_links(brief.get("sources") or []),
            "status": brief.get("research_status"),
        } if brief else None),
        "placeholders": left, "connection": owner_conn, "cap": cap, "stuck": stuck,
        "decks": decks_service.list_decks(db), "deck": deck,
        "attachment": decks_service.attachment_label(deck) if deck else None,
        "deck_missing": deck is not None and not decks_service.is_available(deck),
        "hash_prefix": hash_prefix, "approved_hash": (msg.content_sha256 or "")[:12] or None,
        "can_edit": can_edit,
        # Send exists ONLY for an approved message the acting member owns, with Gmail connected and cap room.
        "can_send": owner and msg.state == "approved" and owner_conn.usable and not cap["reached"],
        "can_retry_known": owner and msg.state == "failed" and not msg.send_outcome_unknown and msg.content_sha256 is not None
        and owner_conn.usable and not cap["reached"],
        "can_retry_unknown": owner and msg.state == "failed" and msg.send_outcome_unknown and msg.content_sha256 is not None
        and owner_conn.usable and not cap["reached"],
        "can_approve": owner and msg.state in ("draft", "edited"),
        "can_reject": owner and msg.state in messages.EDITABLE_STATES and not (msg.state == "failed" and msg.send_outcome_unknown),
        "section": "outbox",
        "replies": [
            v for v in replies_service.list_replies(db, member, everyone=True) if v.message.id == msg.id
        ],
        "suppressed_to": suppressions_service.suppressed_among(db, msg.to_addrs, msg.cc_addrs),
    }  # fmt: skip


@router.get("/outreach/messages/{msg_id}", response_class=HTMLResponse)
def message_page(
    request: Request, msg_id: PathId, db: Session = Depends(get_db), member: TeamMember = Depends(get_acting_member)
) -> HTMLResponse:
    msg = messages.get_message(db, msg_id, member)
    return _page(request, db, "outreach/message.html", _message_ctx(db, msg, member))


def _msg_url(msg_id: PathId) -> str:
    return f"/outreach/messages/{msg_id}"


@router.post("/outreach/messages/{msg_id}/edit", response_model=None)
def edit_message(
    request: Request,
    msg_id: PathId,
    to: str = Form(""),
    cc: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    deck_id: str | None = Form(None),
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
) -> Response:
    # A post WITHOUT a deck_id field keeps the attachment (it used to default to "none" and silently detach it);
    # only an explicit "none" removes it.
    deck_change: Any = messages.UNSET if deck_id is None else _opt_int(deck_id)

    def act() -> str:
        before = messages.get_message(db, msg_id).state
        after = messages.update_message(
            db, msg_id, member, to=to, cc=cc, subject=subject, body=body, deck_id=deck_change
        ).state  # fmt: skip
        if before == "approved" and after != "approved":
            return "Saved. Because you edited an approved message, its approval was reset - approve it again before sending."
        return "Saved."

    return _guard(request, _msg_url(msg_id), act)


@router.post("/outreach/messages/{msg_id}/approve", response_model=None)
def approve_message(
    request: Request, msg_id: PathId, db: Session = Depends(get_db), member: TeamMember = Depends(get_acting_member)
) -> Response:
    def act() -> str:
        msg = messages.approve(db, msg_id, member)
        return f"Approved exactly this version (hash {(msg.content_sha256 or '')[:12]}). Nothing has been sent yet."

    return _guard(request, _msg_url(msg_id), act)


@router.post("/outreach/messages/{msg_id}/reject", response_model=None)
def reject_message(
    request: Request,
    msg_id: PathId,
    reason: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
) -> Response:
    def act() -> str:
        messages.reject(db, msg_id, member, reason=reason or None)
        return "Rejected. It will not be sent."

    return _guard(request, _msg_url(msg_id), act)


@router.post("/outreach/messages/{msg_id}/send", response_model=None)
def send_message(
    request: Request, msg_id: PathId, db: Session = Depends(get_db), member: TeamMember = Depends(get_acting_member)
) -> Response:
    def act() -> str:
        before = messages.get_message(db, msg_id).state
        msg = messages.send(db, msg_id, member)
        if msg.state == "sent":
            if before in ("sent", "sending"):
                return "This message was already sent - nothing was sent again."
            return f"Sent from {msg.from_addr}."
        if msg.state == "sending":
            return "This message is already being sent - nothing was sent again."
        if msg.state == "failed":
            raise DomainError("send_failed", msg.error or "The send failed - see the timeline.", 409)
        return "Nothing was sent."

    return _guard(request, _msg_url(msg_id), act)


@router.post("/outreach/messages/{msg_id}/retry", response_model=None)
def retry_message(
    request: Request,
    msg_id: PathId,
    confirm_not_sent: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
) -> Response:
    def act() -> str:
        msg = messages.retry_send(db, msg_id, member, confirm_not_sent == "1")
        if msg.state == "failed":
            raise DomainError("send_failed", msg.error or "The retry failed - see the timeline.", 409)
        return f"Sent from {msg.from_addr}."

    return _guard(request, _msg_url(msg_id), act)


# ---------------------------------------------------------------------- templates
def _template_ctx(db: Session, edit_id: int | None) -> dict[str, Any]:
    editing = templates.get_template(db, edit_id) if edit_id else None
    offs = offerings_service.list_offerings(db)
    return {
        "templates": templates.list_templates(db), "editing": editing, "offerings": offs,
        "offering_names": {o.id: o.name for o in offs}, "variables": templates.VARIABLES, "section": "templates",
    }  # fmt: skip


@router.get("/outreach/templates", response_class=HTMLResponse)
def templates_page(
    request: Request, edit: str | None = None, db: Session = Depends(get_db), member: TeamMember = Depends(get_acting_member)
) -> HTMLResponse:
    return _page(request, db, "outreach/templates.html", _template_ctx(db, _opt_int(edit)))


@router.post("/outreach/templates/save", response_model=None)
def save_template(
    request: Request,
    template_id: str = Form(""),
    name: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    offering_id: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
    _admin: TeamMember = Depends(require_admin),
) -> Response:
    tid = _opt_int(template_id)

    def act() -> str:
        if tid:
            row = templates.update_template(db, tid, name=name, subject=subject, body=body, offering_id=_opt_int(offering_id))
            return f"Template '{row.name}' saved."
        row = templates.create_template(db, name=name, subject=subject, body=body, offering_id=_opt_int(offering_id))
        return f"Template '{row.name}' created."

    return _guard(request, f"/outreach/templates?edit={tid}" if tid else "/outreach/templates", act)


@router.post("/outreach/templates/{template_id}/delete", response_model=None)
def delete_template(
    request: Request, template_id: PathId, db: Session = Depends(get_db), member: TeamMember = Depends(get_acting_member),
    _admin: TeamMember = Depends(require_admin)
) -> Response:
    def act() -> str:
        name = templates.get_template(db, template_id).name
        templates.delete_template(db, template_id)
        return f"Template '{name}' deleted."

    return _guard(request, "/outreach/templates", act)


@router.post("/outreach/templates/preview", response_class=HTMLResponse)
def preview_template(
    request: Request,
    subject: str = Form(""),
    body: str = Form(""),
    offering_id: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
) -> HTMLResponse:
    from sales0.web.context import render_fragment

    oid = _opt_int(offering_id)
    names = {o.id: o.name for o in offerings_service.list_offerings(db)}
    values = templates.template_values(
        first_name=SAMPLE_VALUES["first_name"], company=SAMPLE_VALUES["company"], requirement=SAMPLE_VALUES["requirement"],
        sender_name=member.name, offering=names.get(oid) if oid else "AI corporate workshops",
    )  # fmt: skip
    rs, rb = templates.render(subject, values), templates.render(body, values)
    return render_fragment(
        request, "outreach/_preview.html",
        {"subject": rs.text, "body": rb.text, "unknown": list(dict.fromkeys(rs.unknown + rb.unknown)), "sample": SAMPLE_VALUES, "sender": member.name},
    )  # fmt: skip


# ---------------------------------------------------------------------- decks
@router.get("/outreach/decks", response_class=HTMLResponse)
def decks_page(request: Request, db: Session = Depends(get_db), member: TeamMember = Depends(get_acting_member)) -> HTMLResponse:
    offs = offerings_service.list_offerings(db)
    return _page(
        request, db, "outreach/decks.html",
        {
            "decks": decks_service.list_decks(db), "offerings": offs, "offering_names": {o.id: o.name for o in offs},
            "max_mb": get_settings().deck_max_bytes // (1024 * 1024), "section": "decks",
        },
    )  # fmt: skip


@router.post("/outreach/decks", response_model=None)
def upload_deck(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(""),
    offering_id: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
    _admin: TeamMember = Depends(require_admin),
) -> Response:
    def act() -> str:
        limit = get_settings().deck_max_bytes
        data = file.file.read(limit + 1)  # never buffer more than the cap + 1 byte
        row = decks_service.upload_deck(
            db, filename=file.filename, data=data, name=name or None, offering_id=_opt_int(offering_id)
        )  # fmt: skip
        return f"Deck '{row.name}' uploaded."

    return _guard(request, "/outreach/decks", act)


@router.post("/outreach/decks/{deck_id}/offering", response_model=None)
def set_deck_offering(
    request: Request,
    deck_id: PathId,
    offering_id: str = Form(""),
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_acting_member),
    _admin: TeamMember = Depends(require_admin),
) -> Response:
    def act() -> str:
        row = decks_service.set_deck_offering(db, deck_id, _opt_int(offering_id))
        return f"Deck '{row.name}' updated."

    return _guard(request, "/outreach/decks", act)


@router.post("/outreach/decks/{deck_id}/delete", response_model=None)
def delete_deck(
    request: Request, deck_id: PathId, db: Session = Depends(get_db), member: TeamMember = Depends(get_acting_member),
    _admin: TeamMember = Depends(require_admin)
) -> Response:
    def act() -> str:
        name = decks_service.get_deck(db, deck_id).name
        decks_service.delete_deck(db, deck_id)
        return f"Deck '{name}' deleted. Messages already approved with it can still be sent."

    return _guard(request, "/outreach/decks", act)


@router.get("/outreach/decks/{deck_id}/file")
def deck_file(deck_id: PathId, db: Session = Depends(get_db), member: TeamMember = Depends(get_acting_member)) -> Response:
    deck = decks_service.get_deck(db, deck_id)
    data = decks_service.read_deck_bytes(deck)
    return Response(
        data, media_type=decks_service.PDF_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{deck.filename}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )  # fmt: skip


__all__ = ["router", "get_llm"]
