"""Keywords page: every search keyword with what it found (performance tier, downstream outcomes), easy manual
editing (inline edit, bulk add, bulk pause / activate / move / delete) and AI keyword ideas (``agent.keyword_ideas``,
one call on the strong model) reviewed as a list the team ticks and adds.

Reads are open to every member; changes need an admin (same rule as Setup > Keywords). htmx answers re-render the
results region (+ a toast); plain forms get 303 + flash; JSON gets the envelope.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.agent import keyword_ideas
from sales0.db.engine import get_db
from sales0.db.models import Offering, TeamMember
from sales0.errors import DomainError
from sales0.llm import LLMAuthError, LLMClient, LLMModelNotFound, get_llm_client
from sales0.services import keyword_insights as insights
from sales0.services import keywords as keywords_service
from sales0.web.acting import clear_flash, pop_flash, require_admin
from sales0.web.context import PathId, cost_badge_oob, crm_guard, flash_redirect, is_htmx, render_html, render_page, toast_html
from sales0.web.envelope import ok as envelope_ok
from sales0.web.envelope import wants_json

router = APIRouter()
BACK = "/keywords"


def get_llm() -> LLMClient:
    """LLM seam (``app.dependency_overrides[get_llm]`` in tests)."""
    return get_llm_client()


def _params(raw: dict[str, Any]) -> dict[str, str]:
    def pick(name: str, allowed: tuple[str, ...], default: str = "") -> str:
        v = str(raw.get(name) or "")
        return v if v in allowed else default

    offering = str(raw.get("offering") or "")
    return {
        "q": str(raw.get("q") or "").strip()[:100],
        "view": pick("view", insights.VIEWS, "tier"),
        "sort": pick("sort", insights.SORTS, "tier"),
        "tier": pick("tier", insights.TIER_ORDER),
        "status": pick("status", ("active", "paused")),
        "offering": offering if offering == "none" or offering.isdigit() else "",
    }


def _ctx(db: Session, params: dict[str, str]) -> dict[str, Any]:
    stats = insights.keyword_stats(db)
    shown = insights.filtered(stats, q=params["q"], tier=params["tier"], status=params["status"], offering=params["offering"],
                              sort=params["sort"])
    offerings = list(db.scalars(select(Offering).order_by(Offering.sort_order, Offering.id)))
    return {
        "params": params, "groups": insights.grouped(shown, params["view"]), "shown": len(shown), "total": len(stats),
        "tier_counts": insights.tier_counts(stats), "tier_labels": insights.TIER_LABELS, "tiers": insights.TIER_ORDER,
        "offerings": offerings, "views": insights.VIEWS, "sorts": insights.SORTS,
    }


def _results(request: Request, db: Session, params: dict[str, str], message: str | None = None, *, oob: bool = False) -> HTMLResponse:
    body = render_html(request, "keywords/_results.html", {**_ctx(db, params), "oob": oob})
    return HTMLResponse(body + (toast_html(message) if message else ""))


def _back_params(raw: str) -> dict[str, str]:
    from urllib.parse import parse_qsl

    return _params(dict(parse_qsl(raw or "")))


def _done(request: Request, db: Session, back_qs: str, message: str, data: Any = None) -> Response:
    db.commit()
    if wants_json(request):
        return envelope_ok(data if data is not None else {"message": message})
    if is_htmx(request):
        return _results(request, db, _back_params(back_qs), message, oob=True)
    return flash_redirect(BACK + (f"?{back_qs}" if back_qs else ""), message)


@router.get("/keywords", response_class=HTMLResponse)
def keywords_page(request: Request, db: Session = Depends(get_db)) -> Response:
    params = _params(dict(request.query_params))
    if wants_json(request):
        stats = insights.filtered(insights.keyword_stats(db), **{k: params[k] for k in ("q", "tier", "status", "offering", "sort")})
        return envelope_ok([{**{f: getattr(s, f) for f in ("id", "text", "active", "offering_id", "offering_name", "tier", "scanned",
                                                           "relevant", "found", "accepted", "contacted", "leads", "won")},
                             "rate": round(s.rate, 4)} for s in stats])
    if is_htmx(request) and request.headers.get("HX-Target") == "keyword-results":
        return _results(request, db, params)
    flash = pop_flash(request)
    response = render_page(request, db, "keywords/page.html", {**_ctx(db, params), "flash": flash})
    if flash:
        clear_flash(response)
    return response


@router.post("/keywords/add", response_model=None)
def bulk_add(
    request: Request, texts: str = Form(""), offering_id: str = Form(""), back: str = Form(""),
    db: Session = Depends(get_db), admin: TeamMember = Depends(require_admin),
) -> Response:
    def act() -> Response:
        lines = [part for line in texts.replace("\r", "\n").split("\n") for part in line.split(",")]
        if not any(p.strip() for p in lines):
            raise DomainError("keyword_invalid", "Type at least one keyword (one per line).", 400)
        result = keywords_service.bulk_add(db, lines, offering_id=int(offering_id) if offering_id.isdigit() else None,
                                           actor_user_id=admin.id)
        return _done(request, db, back, result.summary(), {"added": result.added, "duplicates": result.duplicates,
                                                           "invalid": result.invalid})

    return crm_guard(request, BACK, act, db)


@router.post("/keywords/bulk", response_model=None)
def bulk_action(
    request: Request, keyword_ids: list[int] = Form(default_factory=list), action: str = Form(""),
    move_to: str = Form(""), back: str = Form(""), db: Session = Depends(get_db), admin: TeamMember = Depends(require_admin),
) -> Response:
    def act() -> Response:
        target = int(move_to) if move_to.isdigit() else None
        n = keywords_service.bulk_update(db, keyword_ids, action, offering_id=target, actor_user_id=admin.id)
        verb = {"pause": "Paused", "activate": "Activated", "move": "Moved", "delete": "Removed"}.get(action, "Updated")
        return _done(request, db, back, f"{verb} {n} keyword{'' if n == 1 else 's'}.", {"changed": n})

    return crm_guard(request, BACK, act, db)


@router.post("/keywords/{keyword_id}/edit", response_model=None)
def edit_keyword(
    request: Request, keyword_id: PathId, text: str = Form(""), offering_id: str = Form(""), back: str = Form(""),
    db: Session = Depends(get_db), admin: TeamMember = Depends(require_admin),
) -> Response:
    def act() -> Response:
        view = keywords_service.update_keyword(db, keyword_id, text, actor_user_id=admin.id)
        if offering_id != "keep":
            keywords_service.bulk_update(db, [keyword_id], "move", offering_id=int(offering_id) if offering_id.isdigit() else None,
                                         actor_user_id=admin.id)
        return _done(request, db, back, f"Saved '{view.text}'.")

    return crm_guard(request, BACK, act, db)


@router.post("/keywords/{keyword_id}/toggle", response_model=None)
def toggle_keyword(
    request: Request, keyword_id: PathId, active: str = Form("true"), back: str = Form(""),
    db: Session = Depends(get_db), admin: TeamMember = Depends(require_admin),
) -> Response:
    def act() -> Response:
        view = keywords_service.set_keyword_active(db, keyword_id, active.strip().lower() in {"true", "1", "on"}, actor_user_id=admin.id)
        return _done(request, db, back, f"'{view.text}' {'active' if view.active else 'paused'}.")

    return crm_guard(request, BACK, act, db)


@router.post("/keywords/ideas", response_model=None)
def ideas(
    request: Request, focus: str = Form(""), db: Session = Depends(get_db), admin: TeamMember = Depends(require_admin),
    llm: LLMClient = Depends(get_llm),
) -> Response:
    """One AI call; the answer is a review list (nothing is stored until the team adds what it ticks)."""

    def act() -> Response:
        try:
            result = keyword_ideas.generate(db, llm, focus=focus)
        except (LLMAuthError, LLMModelNotFound) as exc:
            db.commit()  # keep the error llm_calls row
            raise DomainError("llm_unavailable", f"The AI service refused the request ({type(exc).__name__}) - check the API key and "
                              "SALES0_KEYWORD_IDEAS_MODEL.", 502) from exc
        db.commit()
        offerings = list(db.scalars(select(Offering).order_by(Offering.sort_order, Offering.id)))
        if wants_json(request):
            return envelope_ok({"ideas": [i.__dict__ for i in result.ideas], "retire": [r.__dict__ for r in result.retire],
                                "avoid": result.avoid, "cost_usd_micros": result.cost_usd_micros, "model": result.model})
        html = render_html(request, "keywords/_ideas.html", {"result": result, "offerings": offerings, "focus": focus})
        n = len(result.ideas)
        return HTMLResponse(html + cost_badge_oob(db) + toast_html(f"{n} idea{'' if n == 1 else 's'} ready to review."))

    return crm_guard(request, BACK, act, db)


@router.post("/keywords/ideas/apply", response_model=None)
def apply_ideas(
    request: Request, pick: list[int] = Form(default_factory=list), phrase: list[str] = Form(default_factory=list),
    idea_offering: list[str] = Form(default_factory=list), pause_ids: list[int] = Form(default_factory=list),
    back: str = Form(""), db: Session = Depends(get_db), admin: TeamMember = Depends(require_admin),
) -> Response:
    """Add the ticked ideas (each with its edited text and offering) and pause the ticked keywords to retire."""

    def act() -> Response:
        chosen = [i for i in dict.fromkeys(pick) if 0 <= i < len(phrase)]
        if not chosen and not pause_ids:
            raise DomainError("nothing_selected", "Tick at least one idea to add or keyword to pause.", 400)
        added: list[str] = []
        skipped = 0
        by_offering: dict[int | None, list[str]] = {}
        for i in chosen:
            o = idea_offering[i] if i < len(idea_offering) else ""
            by_offering.setdefault(int(o) if o.isdigit() else None, []).append(phrase[i])
        for oid, texts in by_offering.items():
            r = keywords_service.bulk_add(db, texts, offering_id=oid, actor_user_id=admin.id)
            added += r.added
            skipped += len(r.duplicates) + len(r.invalid)
        paused = keywords_service.bulk_update(db, pause_ids, "pause", actor_user_id=admin.id) if pause_ids else 0
        parts = [f"Added {len(added)} keyword{'' if len(added) == 1 else 's'}"]
        if paused:
            parts.append(f"paused {paused}")
        if skipped:
            parts.append(f"{skipped} skipped (already there or invalid)")
        return _done(request, db, back, ", ".join(parts) + ".", {"added": added, "paused": paused, "skipped": skipped})

    return crm_guard(request, BACK, act, db)


__all__ = ["get_llm", "router"]
