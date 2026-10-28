"""Who is using sales0 right now ("acting as"), plus one-shot flash messages.

With accounts (``spec/capabilities/per-user-accounts.md``) the actor is the logged-in member; an admin may
"act as" another via the header selector (signed cookie, honoured for admins only). In open mode
(no passwords set anywhere) anyone can pick any member, as in Phase 3.

For other routes:  ``member: TeamMember = Depends(get_acting_member)``.
"""

from __future__ import annotations

import base64
import json

from fastapi import Depends, Request, Response
from sqlalchemy.orm import Session

from sales0.db.engine import get_db
from sales0.db.models import TeamMember
from sales0.errors import DomainError
from sales0.services.team import acting as acting_service
from sales0.services.team import crypto
from sales0.web import cookies


def _resolution(request: Request, db: Session) -> acting_service.Resolution:
    cached = getattr(request.state, "acting_resolution", None)
    if cached is not None and cached[0] is db:
        return cached[1]
    res = acting_service.resolve(
        db,
        auth_enabled=bool(getattr(request.state, "auth_enabled", False)),
        login_member_id=getattr(request.state, "login_member_id", None),
        raw_cookie=request.cookies.get(acting_service.COOKIE_NAME),
    )
    request.state.acting_resolution = (db, res)
    return res


def get_login_member_optional(request: Request, db: Session = Depends(get_db)) -> TeamMember | None:
    """The authenticated member (None in open mode)."""
    return _resolution(request, db).login


def get_acting_member_optional(request: Request, db: Session = Depends(get_db)) -> TeamMember | None:
    """Whose name the work carries: the logged-in member, or the one an ADMIN chose to act as."""
    return _resolution(request, db).acting


def get_acting_member(request: Request, db: Session = Depends(get_db)) -> TeamMember:
    member = get_acting_member_optional(request, db)
    if member is None:
        raise DomainError("no_team_member", "There are no active team members yet - add one on the Team page.", 409)
    return member


def require_admin(request: Request, db: Session = Depends(get_db)) -> TeamMember:
    """The REAL user must be an admin (acting as someone else never grants or removes admin rights).
    In open mode the acting member stands in, as before."""
    res = _resolution(request, db)
    who = res.login if res.login is not None else res.acting
    if who is None:
        raise DomainError("no_team_member", "There are no active team members yet - add one on the Team page.", 409)
    if who.role != "admin":
        raise DomainError("forbidden", "Only an admin can do that.", 403)
    return who


def acted_by_id(member: TeamMember | None) -> int | None:
    """The real admin's id when ``member`` is being acted as by someone else, else None."""
    by = getattr(member, "acted_by", None) if member is not None else None
    return by.id if by is not None and member is not None and by.id != member.id else None


def set_acting_cookie(response: Response, member_id: int) -> None:
    cookies.set_cookie(
        response,
        acting_service.COOKIE_NAME,
        acting_service.cookie_value(member_id),
        max_age=acting_service.COOKIE_MAX_AGE,
    )


def set_flash(response: Response, message: str, kind: str = "ok") -> None:
    payload = base64.urlsafe_b64encode(json.dumps({"k": kind, "m": message[:300]}).encode()).decode().rstrip("=")
    cookies.set_cookie(response, acting_service.FLASH_COOKIE, crypto.sign(payload, "flash"), max_age=120)


def pop_flash(request: Request) -> dict[str, str] | None:
    """The pending flash (``{"kind", "message"}``) or None. Caller must also ``clear_flash``."""
    payload = crypto.unsign(request.cookies.get(acting_service.FLASH_COOKIE), "flash")
    if not payload:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return {"kind": "error" if data.get("k") == "error" else "ok", "message": str(data.get("m", ""))}
    except (ValueError, TypeError):
        return None


def clear_flash(response: Response) -> None:
    cookies.delete_cookie(response, acting_service.FLASH_COOKIE)
