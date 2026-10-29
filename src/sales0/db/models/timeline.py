"""Append-only timeline of every change to every CRM record (Phase 2)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc
from sales0.db.models.enums_crm import TIMELINE_KINDS, TIMELINE_SUBJECTS


class TimelineEvent(Base):
    __tablename__ = "timeline_events"
    __table_args__ = (
        Index("ix_timeline_events_prospect_occurred", "prospect_id", "occurred_at"),
        Index("ix_timeline_events_kind_occurred", "kind", "occurred_at"),  # Phase 4 metrics
        Index("ix_timeline_events_company_occurred", "company_id", "occurred_at"),
        Index("ix_timeline_events_contact_occurred", "contact_id", "occurred_at"),
        Index("ix_timeline_events_enquiry_occurred", "enquiry_id", "occurred_at"),
        Index("ix_timeline_events_subject_occurred", "subject_type", "subject_id", "occurred_at"),
        in_check("subject_type", TIMELINE_SUBJECTS, "subject_type"),
        in_check("kind", TIMELINE_KINDS, "kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(12), nullable=False)
    subject_id: Mapped[int] = mapped_column(Integer, nullable=False)
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="SET NULL"))
    enquiry_id: Mapped[int | None] = mapped_column(ForeignKey("enquiries.id", ondelete="SET NULL"))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    actor: Mapped[str] = mapped_column(String(80), nullable=False, default="system", server_default="system")
    actor_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)


__all__ = ["TimelineEvent"]
