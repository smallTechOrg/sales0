"""Team members and their per-member Gmail connection (Phase 3 outreach, slice A)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, String, Text, false, true
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc

MEMBER_ROLES = ("admin", "member")
GMAIL_STATUSES = ("connected", "needs_reauth", "scope_insufficient", "disconnected")


class TeamMember(Base):
    __tablename__ = "team_members"
    __table_args__ = (in_check("role", MEMBER_ROLES, "role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)  # always lowercased
    role: Mapped[str] = mapped_column(String(10), nullable=False, default="member", server_default="member")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    # Phase 5 accounts: team members ARE the users. NULL hash = cannot log in (needs a set-password link).
    password_hash: Mapped[str | None] = mapped_column(String(255))  # argon2id
    password_set_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    must_reset: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class MemberSession(Base):
    """A server-side login session. The cookie carries a random id; only its SHA-256 is stored, so a
    DB leak cannot be replayed, and deleting rows (logout, deactivate, password change) kills sessions."""

    __tablename__ = "member_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("team_members.id", ondelete="CASCADE"), nullable=False, index=True)
    sid_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    prev_sid_hash: Mapped[str | None] = mapped_column(String(64))  # previous cookie id after a rotation (grace)
    prev_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    remember: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    rotated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_ip: Mapped[str | None] = mapped_column(String(64))


class PasswordToken(Base):
    """A one-time set-password link. Only the SHA-256 of the token is stored; single use; expires."""

    __tablename__ = "password_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("team_members.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_by_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"))


class GmailConnection(Base):
    """One Gmail OAuth connection per member. ``token_encrypted`` is a Fernet token of the
    StoredToken JSON; the plaintext refresh token is never stored."""

    __tablename__ = "gmail_connections"
    __table_args__ = (in_check("status", GMAIL_STATUSES, "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("team_members.id", ondelete="CASCADE"), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    scopes: Mapped[list[str] | None] = mapped_column(JSON)
    token_encrypted: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="connected", server_default="connected")
    connected_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)
    last_error: Mapped[str | None] = mapped_column(String(300))


__all__ = ["GMAIL_STATUSES", "MEMBER_ROLES", "GmailConnection", "MemberSession", "PasswordToken", "TeamMember"]
