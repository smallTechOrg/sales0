"""Per-user accounts: login, server-side sessions, one-time set-password links, bootstrap.

Team members ARE the users (see ``spec/capabilities/per-user-accounts.md``). Nothing here sends email: an admin
generates a link and hands it over. Sessions and tokens are stored only as SHA-256 hashes.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc
from sales0.config import Settings, get_settings
from sales0.db.models import MemberSession, PasswordToken, TeamMember
from sales0.errors import DomainError
from sales0.logging import get_logger
from sales0.services._common import now_or
from sales0.services.team import members as members_service
from sales0.services.team import passwords

log = get_logger("sales0.team.accounts")

ROTATE_AFTER = timedelta(minutes=15)  # the cookie id is reissued when older than this
ROTATION_GRACE = timedelta(seconds=60)  # the previous id still works briefly (parallel htmx requests)
OPEN_MODE_MESSAGE = "Open mode - anyone who can reach this app can use it. Add a password on the Users page."


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------- mode
def any_password_set(db: Session) -> bool:
    return db.scalar(select(func.count(TeamMember.id)).where(TeamMember.password_hash.is_not(None))) > 0


def any_admin_has_password(db: Session) -> bool:
    return (
        db.scalar(
            select(func.count(TeamMember.id)).where(TeamMember.role == "admin", TeamMember.password_hash.is_not(None))
        )
        > 0
    )


def auth_enabled(db: Session, settings: Settings | None = None) -> bool:
    """False = open mode (loopback convenience): no member has a password and no bootstrap password is set."""
    settings = settings or get_settings()
    return settings.app_password_set or any_password_set(db)


def bootstrap_admin(db: Session, settings: Settings | None = None, now: datetime | None = None) -> TeamMember | None:
    """``SALES0_APP_PASSWORD`` is the FIRST admin's initial password, only while no admin has one.

    Creates the configured admin (``SALES0_ADMIN_EMAIL``) when missing. Idempotent; once any admin has a
    password the variable is ignored."""
    settings = settings or get_settings()
    if not settings.app_password_set or any_admin_has_password(db):
        return None
    admin = members_service.seed_admin(db, settings)
    if admin.role != "admin":
        admin.role = "admin"
    admin.active = True
    admin.password_hash = passwords.hash_password(settings.app_password.get_secret_value())  # type: ignore[union-attr]
    admin.password_set_at = now_or(now)
    admin.must_reset = False
    db.flush()
    log.info("bootstrap_admin_password_set", member_id=admin.id)
    return admin


# ---------------------------------------------------------------------------- login
def authenticate(
    db: Session, email: str, password: str, settings: Settings | None = None, now: datetime | None = None
) -> TeamMember | None:
    """The member for a correct email+password, else None (the same answer for every failure)."""
    settings = settings or get_settings()
    if settings.app_password_set and not any_admin_has_password(db):
        bootstrap_admin(db, settings, now)
    member = members_service.get_member_by_email(db, email)
    stored = member.password_hash if member is not None else None
    ok = passwords.verify_password(stored, password)
    if member is None or not ok or not member.active or member.must_reset:
        return None
    if stored and passwords.needs_rehash(stored):
        member.password_hash = passwords.hash_password(password)
    member.last_login_at = now_or(now)
    db.flush()
    return member


# ---------------------------------------------------------------------------- sessions
@dataclass(frozen=True)
class SessionInfo:
    member: TeamMember
    session_id: int
    new_sid: str | None  # set when the cookie id was rotated: the caller must reissue the cookie
    remember: bool
    expires_at: datetime


def lifetime(settings: Settings, remember: bool) -> timedelta:
    return timedelta(days=settings.session_remember_days if remember else settings.session_days)


def create_session(
    db: Session, member: TeamMember, settings: Settings, *, remember: bool, ip: str | None = None, now: datetime | None = None
) -> tuple[str, datetime]:
    """A new session; returns (raw cookie id, expires_at)."""
    stamp = now_or(now)
    sid = secrets.token_urlsafe(32)
    expires = stamp + lifetime(settings, remember)
    db.add(
        MemberSession(
            member_id=member.id, sid_hash=hash_token(sid), remember=remember,
            created_at=stamp, rotated_at=stamp, expires_at=expires, last_ip=(ip or "")[:64] or None,
        )
    )
    db.execute(delete(MemberSession).where(MemberSession.expires_at < stamp - timedelta(days=1)))  # housekeeping
    db.flush()
    return sid, expires


def resolve_session(
    db: Session, sid: str | None, settings: Settings, *, ip: str | None = None, now: datetime | None = None
) -> SessionInfo | None:
    """The live session for a cookie id (rotating it when stale), or None."""
    if not sid:
        return None
    stamp = now_or(now)
    digest = hash_token(sid)
    row = db.scalar(
        select(MemberSession).where(
            or_(
                MemberSession.sid_hash == digest,
                (MemberSession.prev_sid_hash == digest) & (MemberSession.prev_expires_at > stamp),
            )
        )
    )
    if row is None or ensure_utc(row.expires_at) <= stamp:
        return None
    member = db.get(TeamMember, row.member_id)
    if member is None or not member.active or member.must_reset or member.password_hash is None:
        return None
    new_sid: str | None = None
    if row.sid_hash == digest and stamp - ensure_utc(row.rotated_at) >= ROTATE_AFTER:
        new_sid = secrets.token_urlsafe(32)
        row.prev_sid_hash, row.prev_expires_at = row.sid_hash, stamp + ROTATION_GRACE
        row.sid_hash, row.rotated_at = hash_token(new_sid), stamp
        row.expires_at = stamp + lifetime(settings, row.remember)
        if ip:
            row.last_ip = ip[:64]
        db.flush()
    return SessionInfo(member, row.id, new_sid, row.remember, ensure_utc(row.expires_at))


def revoke_session(db: Session, sid: str | None) -> None:
    if sid:
        db.execute(delete(MemberSession).where(MemberSession.sid_hash == hash_token(sid)))


def revoke_member_sessions(db: Session, member_id: int, *, except_session_id: int | None = None) -> int:
    stmt = delete(MemberSession).where(MemberSession.member_id == member_id)
    if except_session_id is not None:
        stmt = stmt.where(MemberSession.id != except_session_id)
    return db.execute(stmt).rowcount or 0


# ---------------------------------------------------------------------------- passwords + links
def _apply_password(db: Session, member: TeamMember, new_password: str, settings: Settings, now: datetime | None) -> None:
    passwords.validate_new_password(new_password, settings)
    member.password_hash = passwords.hash_password(new_password)
    member.password_set_at = now_or(now)
    member.must_reset = False
    db.execute(
        update(PasswordToken)
        .where(PasswordToken.member_id == member.id, PasswordToken.used_at.is_(None))
        .values(used_at=now_or(now))
    )
    db.flush()


def issue_set_password_link(
    db: Session, member: TeamMember, by: TeamMember | None, settings: Settings | None = None, now: datetime | None = None
) -> tuple[str, datetime]:
    """Force a reset: invalidate older links, revoke sessions, block the old password, return (raw token, expiry).

    The raw token is shown to the admin ONCE; only its hash is stored."""
    settings = settings or get_settings()
    stamp = now_or(now)
    db.execute(
        update(PasswordToken).where(PasswordToken.member_id == member.id, PasswordToken.used_at.is_(None)).values(used_at=stamp)
    )
    raw = secrets.token_urlsafe(32)
    expires = stamp + timedelta(hours=settings.password_reset_ttl_h)
    db.add(
        PasswordToken(
            member_id=member.id, token_hash=hash_token(raw), created_at=stamp, expires_at=expires,
            created_by_member_id=by.id if by is not None else None,
        )
    )
    if member.password_hash is not None:
        member.must_reset = True
    revoke_member_sessions(db, member.id)
    db.flush()
    log.info("set_password_link_issued", member_id=member.id, by_member_id=by.id if by else None)
    return raw, expires


def _live_token(db: Session, raw: str | None, now: datetime | None) -> PasswordToken | None:
    if not raw:
        return None
    row = db.scalar(select(PasswordToken).where(PasswordToken.token_hash == hash_token(raw)))
    if row is None or row.used_at is not None or ensure_utc(row.expires_at) <= now_or(now):
        return None
    return row


def peek_token(db: Session, raw: str | None, now: datetime | None = None) -> TeamMember | None:
    """The member a still-valid link belongs to (does not consume it)."""
    row = _live_token(db, raw, now)
    member = db.get(TeamMember, row.member_id) if row else None
    return member if member is not None and member.active else None


def set_password_with_token(
    db: Session, raw: str | None, new_password: str, settings: Settings | None = None, now: datetime | None = None
) -> TeamMember:
    settings = settings or get_settings()
    row = _live_token(db, raw, now)
    member = db.get(TeamMember, row.member_id) if row else None
    if row is None or member is None or not member.active:
        raise DomainError("link_invalid", "This link has expired or was already used - ask an admin for a new one.", 400)
    passwords.validate_new_password(new_password, settings)
    claimed = db.execute(
        update(PasswordToken).where(PasswordToken.id == row.id, PasswordToken.used_at.is_(None)).values(used_at=now_or(now))
    ).rowcount
    if claimed != 1:
        raise DomainError("link_invalid", "This link has expired or was already used - ask an admin for a new one.", 400)
    _apply_password(db, member, new_password, settings, now)
    revoke_member_sessions(db, member.id)
    log.info("password_set_via_link", member_id=member.id)
    return member


def change_own_password(
    db: Session, member: TeamMember, current: str, new_password: str, settings: Settings | None = None,
    *, keep_session_id: int | None = None, now: datetime | None = None,
) -> None:
    settings = settings or get_settings()
    if not passwords.verify_password(member.password_hash, current):
        raise DomainError("password_wrong", "Your current password is not right.", 400)
    _apply_password(db, member, new_password, settings, now)
    revoke_member_sessions(db, member.id, except_session_id=keep_session_id)
    log.info("password_changed", member_id=member.id)
