"""The "acting as" member: a signed cookie naming who is using this (login-less) app."""

from __future__ import annotations

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
