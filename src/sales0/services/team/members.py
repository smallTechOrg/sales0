"""Team members: seed the first admin, list, get, add, activate/deactivate."""

from __future__ import annotations

import re

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from sales0.config import Settings, get_settings
from sales0.db.models import MemberSession, TeamMember
from sales0.db.models.team import MEMBER_ROLES
from sales0.errors import DomainError
from sales0.logging import get_logger
from sales0.services._common import not_found

log = get_logger("sales0.team.members")

_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def normalise_email(email: str) -> str:
    return email.strip().lower()


def seed_admin(db: Session, settings: Settings | None = None) -> TeamMember:
    """Create the configured first admin if no member has that email. Idempotent."""
    settings = settings or get_settings()
    email = normalise_email(settings.admin_email)
    existing = db.execute(select(TeamMember).where(TeamMember.email == email)).scalar_one_or_none()
    if existing is not None:
        return existing
    member = TeamMember(name=settings.admin_name.strip() or "Admin", email=email, role="admin", active=True)
    db.add(member)
    db.flush()
    log.info("admin_seeded", member_id=member.id)
    return member


def list_members(db: Session, *, include_inactive: bool = True) -> list[TeamMember]:
    stmt = select(TeamMember).order_by(TeamMember.id)
    if not include_inactive:
        stmt = stmt.where(TeamMember.active.is_(True))
    return list(db.execute(stmt).scalars())


def get_member(db: Session, member_id: int) -> TeamMember:
    member = db.get(TeamMember, member_id)
    if member is None:
        raise not_found()
    return member


def get_member_by_email(db: Session, email: str) -> TeamMember | None:
    return db.execute(select(TeamMember).where(TeamMember.email == normalise_email(email))).scalar_one_or_none()


def first_active_admin(db: Session) -> TeamMember | None:
    return db.execute(
        select(TeamMember).where(TeamMember.role == "admin", TeamMember.active.is_(True)).order_by(TeamMember.id)
    ).scalars().first()


def add_member(db: Session, name: str, email: str, role: str = "member") -> TeamMember:
    name = " ".join(name.split())
    email = normalise_email(email)
    if not name or len(name) > 80:
        raise DomainError("member_invalid", "Enter the member's name (1-80 characters).", 400)
    if not _EMAIL.match(email) or len(email) > 254:
        raise DomainError("member_invalid", "Enter a valid email address - it must be the member's Gmail address.", 400)
    if role not in MEMBER_ROLES:
        raise DomainError("member_invalid", "Role must be admin or member.", 400)
    if get_member_by_email(db, email) is not None:
        raise DomainError("member_exists", f"{email} is already on the team.", 409)
    member = TeamMember(name=name, email=email, role=role, active=True)
    db.add(member)
    db.flush()
    return member


def _other_active_admins(db: Session, member: TeamMember) -> list[TeamMember]:
    return [m for m in list_members(db, include_inactive=False) if m.role == "admin" and m.id != member.id]


def set_active(db: Session, member_id: int, active: bool, *, by: TeamMember | None = None) -> TeamMember:
    """Activate/deactivate. Deactivating revokes the member's sessions at once. Never the last active admin,
    and never yourself (``by``) - that would lock you out."""
    member = get_member(db, member_id)
    if not active:
        if by is not None and by.id == member.id:
            raise DomainError("self_lockout", "You cannot deactivate your own account - ask another admin.", 409)
        if member.role == "admin" and member.active and not _other_active_admins(db, member):
            raise DomainError("last_admin", "You cannot deactivate the only active admin.", 409)
    member.active = active
    if not active:
        db.execute(delete(MemberSession).where(MemberSession.member_id == member.id))
    db.flush()
    return member


def set_role(db: Session, member_id: int, role: str, *, by: TeamMember | None = None) -> TeamMember:
    """Change role. The last active admin cannot be demoted (the app would have no administrator)."""
    if role not in MEMBER_ROLES:
        raise DomainError("member_invalid", "Role must be admin or member.", 400)
    member = get_member(db, member_id)
    if member.role == "admin" and role != "admin" and member.active and not _other_active_admins(db, member):
        raise DomainError("last_admin", "You cannot demote the only active admin.", 409)
    member.role = role
    db.flush()
    return member
