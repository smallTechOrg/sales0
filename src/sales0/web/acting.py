"""Who is using sales0 right now ("acting as"), plus one-shot flash messages.

There is no login: the header selector sets a signed cookie naming the team member. Anyone
who can reach the app can pick any member, so this is a convenience for a trusted team, not
authentication.

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


def get_acting_member_optional(request: Request, db: Session = Depends(get_db)) -> TeamMember | None:
    return acting_service.resolve_acting_member(db, request.cookies.get(acting_service.COOKIE_NAME))


def get_acting_member(request: Request, db: Session = Depends(get_db)) -> TeamMember:
    member = get_acting_member_optional(request, db)
    if member is None:
        raise DomainError("no_team_member", "There are no active team members yet - add one on the Team page.", 409)
    return member


def set_acting_cookie(response: Response, member_id: int) -> None:
    response.set_cookie(
        acting_service.COOKIE_NAME,
        acting_service.cookie_value(member_id),
        max_age=acting_service.COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


def set_flash(response: Response, message: str, kind: str = "ok") -> None:
    payload = base64.urlsafe_b64encode(json.dumps({"k": kind, "m": message[:300]}).encode()).decode().rstrip("=")
    response.set_cookie(
        acting_service.FLASH_COOKIE, crypto.sign(payload, "flash"), max_age=120, httponly=True, samesite="lax", path="/"
    )


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
    response.delete_cookie(acting_service.FLASH_COOKIE, path="/")
