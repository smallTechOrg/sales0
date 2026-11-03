"""q23: company offerings (a company can fit several offerings), the reusable reminders module, the free-text
follow-up log, and long text settings (the team's drafting instructions)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, now_utc


class CompanyOffering(Base):
    """An offering the company is a fit for. ``source``: ``manual`` (a person) or ``ai`` (refresh / research)."""

    __tablename__ = "company_offerings"
    __table_args__ = (UniqueConstraint("company_id", "offering_id", name="uq_company_offerings"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    offering_id: Mapped[int] = mapped_column(ForeignKey("offerings.id", ondelete="CASCADE"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="manual", server_default="manual")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)


class Reminder(Base):
    """A dated "do this" for anything (``subject_type`` + ``subject_id``, e.g. ``prospect`` / ``company``); the
    company / prospect columns let the Due panel and pages find it. Done or cancelled = closed."""

    __tablename__ = "reminders"
    __table_args__ = (Index("ix_reminders_open_due", "done_at", "cancelled_at", "due_on"),
                      Index("ix_reminders_subject", "subject_type", "subject_id"))

    id: Mapped[int] = mapped_column(primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_id: Mapped[int] = mapped_column(Integer, nullable=False)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    due_on: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default="")
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual", server_default="manual")
    created_by: Mapped[str] = mapped_column(String(80), nullable=False, default="system", server_default="system")
    owner_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"))
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    done_by: Mapped[str | None] = mapped_column(String(80))
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)


class FollowUpNote(Base):
    """One entry of a prospect's follow-up log: what the person typed, and the AI's structured reading of it."""

    __tablename__ = "follow_up_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), nullable=False, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    how: Mapped[str] = mapped_column(String(20), nullable=False, default="other", server_default="other")
    notes: Mapped[str] = mapped_column(String(1000), nullable=False, default="", server_default="")
    happened_on: Mapped[date] = mapped_column(Date, nullable=False)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    reminder_id: Mapped[int | None] = mapped_column(ForeignKey("reminders.id", ondelete="SET NULL"))
    structured_by: Mapped[str] = mapped_column(String(10), nullable=False, default="ai", server_default="ai")  # ai | fallback
    actor: Mapped[str] = mapped_column(String(80), nullable=False, default="team", server_default="team")
    actor_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)


class TextSetting(Base):
    """A long, team-editable text setting (``app_meta`` caps values at 500 characters)."""

    __tablename__ = "text_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    updated_by: Mapped[str | None] = mapped_column(String(80))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)
