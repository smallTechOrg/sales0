"""The global offering filter (SMA-32): one offering picked in the page header narrows the Inbox, Pipeline, Companies
and Analytics to that offering on every page, until it is cleared.

The choice is a plain cookie (``sales0_offering`` = the offering id; SameSite=Lax, path /), set by
``POST /offering-filter`` - a form with a select and a submit button, so it works without JavaScript. The cookie is
only a default: a page's own explicit ``offering`` query parameter wins (``offering=all`` = explicitly every offering).
An unknown or malformed cookie is ignored.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from sales0.db.engine import get_db
from sales0.db.models import Offering
from sales0.web import cookies
from sales0.web.auth import safe_next

COOKIE_NAME = "sales0_offering"
COOKIE_MAX_AGE = 365 * 24 * 3600
ALL = "all"  # an explicit per-page "every offering" (overrides the cookie)
MAX_ID = 2**31 - 1
# Query parameters dropped from ``next`` when the global filter changes: a page's explicit offering would hide the new
# choice, and the page number belongs to the old result set.
_DROP_FROM_NEXT = ("offering", "page")

router = APIRouter()


def _as_id(value: str | None) -> int | None:
    text = (value or "").strip()
    if not text.isdigit():
        return None
    number = int(text)
    return number if 0 < number <= MAX_ID else None


def current_offering(request: Request, db: Session) -> Offering | None:
    """The offering the global filter is set to, or None (no cookie, or one naming no offering). Cached per request."""
    cached = getattr(request.state, "offering_filter", None)
    if cached is not None and cached[0] is db:
        return cached[1]
    oid = _as_id(request.cookies.get(COOKIE_NAME))
    offering = db.get(Offering, oid) if oid is not None else None
    request.state.offering_filter = (db, offering)
    return offering


def current_offering_id(request: Request, db: Session) -> int | None:
    """The validated offering id of the global filter (an existing offering), else None."""
    offering = current_offering(request, db)
    return offering.id if offering is not None else None


def resolve(raw: Any, default_id: int | None) -> tuple[str, int | None]:
    """A page's ``offering`` query value -> (the value to keep in the page's params, the offering id to filter by).

    An explicit id wins; ``all`` is explicitly no filter; anything else falls back to the global filter."""
    text = str(raw or "").strip()
    if text == ALL:
        return ALL, None
    explicit = _as_id(text)
    if explicit is not None:
        return str(explicit), explicit
    return (str(default_id), default_id) if default_id else ("", None)


def next_without_offering(value: str | None) -> str:
    """A same-site path to go back to (open-redirect safe) without the page's own offering / page parameters."""
    target = safe_next(value)
    parts = urlsplit(target)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in _DROP_FROM_NEXT])
    return parts.path + (f"?{query}" if query else "")


def shell_view(request: Request, db: Session) -> dict[str, Any]:
    """The header selector: every offering (inactive ones marked) and the active one."""
    from sales0.services import offerings as offerings_service

    current = current_offering(request, db)
    path = request.url.path
    query = request.url.query
    return {
        "options": offerings_service.list_offerings(db, include_inactive=True),
        "current": current,
        "next": next_without_offering(path + (f"?{query}" if query else "")),
    }


@router.post("/offering-filter")
def set_offering_filter(
    request: Request,
    offering_id: str = Form(""),
    next: str = Form("/inbox"),
    db: Session = Depends(get_db),
) -> Response:
    """Set (an offering id) or clear (empty / ``all`` / an unknown id) the global offering filter, then go back."""
    response = RedirectResponse(next_without_offering(next), status_code=303)
    oid = _as_id(offering_id)
    offering = db.get(Offering, oid) if oid is not None else None
    if offering is None:
        cookies.delete_cookie(response, COOKIE_NAME, request, httponly=True, samesite="lax")
    else:
        cookies.set_cookie(response, COOKIE_NAME, str(offering.id), request, max_age=COOKIE_MAX_AGE)
    return response


__all__ = [
    "ALL",
    "COOKIE_NAME",
    "current_offering",
    "current_offering_id",
    "next_without_offering",
    "resolve",
    "router",
    "set_offering_filter",
    "shell_view",
]
