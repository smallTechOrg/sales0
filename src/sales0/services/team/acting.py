"""Who is acting: the logged-in member, or - for admins only - another member they chose to act as.

With accounts enabled the logged-in member (``login``) is the actor. An ADMIN may pick someone else in the
header ("act as"); that choice is honoured only for admins and is stamped on the returned member as
``acted_by`` (the real user), so audit records can say "X as Y". Non-admins can never act as someone else.
In open mode (no passwords anywhere) nobody is logged in and the cookie selector is free, as before.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from sales0.db.models import TeamMember
from sales0.services.team import crypto
from sales0.services.team.members import first_active_admin

COOKIE_NAME = "sales0_acting"
FLASH_COOKIE = "sales0_flash"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def cookie_value(member_id: int) -> str:
    return crypto.sign(str(member_id), "acting")


def member_id_from_cookie(raw: str | None) -> int | None:
    value = crypto.unsign(raw, "acting")
    return int(value) if value and value.isdigit() else None


def resolve_acting_member(db: Session, raw_cookie: str | None) -> TeamMember | None:
    """The cookie's member when valid and active, else the first active admin (None if no members)."""
    member_id = member_id_from_cookie(raw_cookie)
    if member_id is not None:
        member = db.get(TeamMember, member_id)
        if member is not None and member.active:
            return member
    return first_active_admin(db)


@dataclass(frozen=True)
class Resolution:
    login: TeamMember | None  # the authenticated member (None in open mode)
    acting: TeamMember | None  # whose name/credit the work carries


def resolve(db: Session, *, auth_enabled: bool, login_member_id: int | None, raw_cookie: str | None) -> Resolution:
    if not auth_enabled:
        return Resolution(None, resolve_acting_member(db, raw_cookie))
    login = db.get(TeamMember, login_member_id) if login_member_id else None
    if login is None or not login.active:
        return Resolution(None, None)
    acting = login
    if login.role == "admin":
        chosen_id = member_id_from_cookie(raw_cookie)
        if chosen_id is not None and chosen_id != login.id:
            chosen = db.get(TeamMember, chosen_id)
            if chosen is not None and chosen.active:
                chosen.acted_by = login  # type: ignore[attr-defined]  # transient, not a column
                acting = chosen
    return Resolution(login, acting)
