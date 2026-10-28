"""CRM aggregates (Phase 2): companies, prospects, leads, clients, follow-up reminders, flags."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc
from sales0.db.models.enums_crm import (
    CLIENT_SOURCES,
    CLIENT_STATUSES,
    FLAG_ENTITY_TYPES,
    FLAG_KINDS,
    FLAG_SOURCES,
    FOLLOW_UP_KINDS,
    LEAD_OPENED_BY,
    LEAD_STAGES,
    PROSPECT_KINDS,
    PROSPECT_SOURCES,
    PROSPECT_STAGES,
    PROSPECT_STATUSES,
    STAGE_SOURCES,
)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    website: Mapped[str | None] = mapped_column(String(300))
    website_key: Mapped[str | None] = mapped_column(String(200), index=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    city: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)


class Prospect(Base):
    __tablename__ = "prospects"
    __table_args__ = (
        Index("ix_prospects_status_stage", "status", "stage"),
        in_check("kind", PROSPECT_KINDS, "kind"),
        in_check("stage", PROSPECT_STAGES, "stage"),
        in_check("stage_source", STAGE_SOURCES, "stage_source"),
        in_check("status", PROSPECT_STATUSES, "status"),
        in_check("source", PROSPECT_SOURCES, "source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="company_enquiry", server_default="company_enquiry")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    linkedin_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("linkedin_profiles.id", ondelete="RESTRICT"), unique=True
    )
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )
    origin_enquiry_id: Mapped[int | None] = mapped_column(
        ForeignKey("enquiries.id", ondelete="SET NULL")
    )
    stage: Mapped[str] = mapped_column(String(20), nullable=False, default="new", server_default="new")
    stage_source: Mapped[str] = mapped_column(String(6), nullable=False, default="app", server_default="app")
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="open", server_default="open")
    closed_reason: Mapped[str | None] = mapped_column(String(60))
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    contacted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    contacted_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    responded_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_follow_up_on: Mapped[date | None] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="enquiry", server_default="enquiry")
    reason_url: Mapped[str | None] = mapped_column(String(1000))
    reason_text: Mapped[str | None] = mapped_column(String(500))
    extra_link: Mapped[str | None] = mapped_column(String(1000))
    notes: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(80), nullable=False, default="system", server_default="system")
    actor_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"))
    # sheet columns (data.md -> Sheet-fed tables)
    sheet_status: Mapped[str | None] = mapped_column(String(200))
    sheet_notes: Mapped[str | None] = mapped_column(Text)
    sheet_removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sheet_fields: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        Index("ix_leads_stage_closed_at", "stage", "closed_at"),
        Index(
            "uq_leads_one_open_per_prospect",
            "prospect_id",
            unique=True,
            sqlite_where=text("closed_at IS NULL"),
            postgresql_where=text("closed_at IS NULL"),
        ),
        in_check("stage", LEAD_STAGES, "stage"),
        in_check("stage_source", STAGE_SOURCES, "stage_source"),
        in_check("opened_by", LEAD_OPENED_BY, "opened_by"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), nullable=False, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))
    stage: Mapped[str] = mapped_column(String(20), nullable=False, default="meeting_scheduled", server_default="meeting_scheduled")
    stage_source: Mapped[str] = mapped_column(String(6), nullable=False, default="app", server_default="app")
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    meeting_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    proposal_sent_on: Mapped[date | None] = mapped_column(Date)
    value_estimate: Mapped[int | None] = mapped_column(Integer)
    outcome_note: Mapped[str | None] = mapped_column(String(500))
    opened_by: Mapped[str] = mapped_column(String(10), nullable=False, default="manual", server_default="manual")
    actor: Mapped[str] = mapped_column(String(80), nullable=False, default="system", server_default="system")
    actor_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)


class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (
        Index("ix_clients_status", "status"),
        in_check("status", CLIENT_STATUSES, "status"),
        in_check("source", CLIENT_SOURCES, "source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="active", server_default="active")
    source: Mapped[str] = mapped_column(String(15), nullable=False, default="manual", server_default="manual")
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"), unique=True)
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="SET NULL"))
    started_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(80), nullable=False, default="system", server_default="system")
    actor_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)


class FollowUp(Base):
    __tablename__ = "follow_ups"
    __table_args__ = (
        Index("ix_follow_ups_due", "due_on", "done_at", "cancelled_at"),
        in_check("kind", FOLLOW_UP_KINDS, "kind"),
        CheckConstraint(
            "(prospect_id IS NOT NULL AND lead_id IS NULL) OR (prospect_id IS NULL AND lead_id IS NOT NULL)",
            name="one_owner",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    due_on: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(String(300))
    auto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    done_actor: Mapped[str | None] = mapped_column(String(80))
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancel_reason: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)


class Flag(Base):
    __tablename__ = "flags"
    __table_args__ = (
        Index("ix_flags_kind_cleared_at", "kind", "cleared_at"),
        Index(
            "uq_flags_active",
            "entity_type",
            "entity_id",
            "kind",
            unique=True,
            sqlite_where=text("cleared_at IS NULL"),
            postgresql_where=text("cleared_at IS NULL"),
        ),
        in_check("entity_type", FLAG_ENTITY_TYPES, "entity_type"),
        in_check("kind", FLAG_KINDS, "kind"),
        in_check("source", FLAG_SOURCES, "source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(10), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(5), nullable=False, default="auto", server_default="auto")
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    cleared_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cleared_by: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)


__all__ = ["Client", "Company", "Flag", "FollowUp", "Lead", "Prospect"]
