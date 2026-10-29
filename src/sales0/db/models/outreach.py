"""Outreach persistence (Phase 3, defined in slice A so the migration is single).

Phase 2 (migration p6_crm_5b2e7d14) added the foreign keys on ``outreach_messages.prospect_id`` /
``enquiry_id`` / ``company_id`` and ``company_briefs.prospect_id`` / ``enquiry_id`` (all ``SET NULL``).
``offering_id`` columns on templates/decks stay plain integers.
``outreach_events`` is an append-only audit trail: never update or delete its rows.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, String, Text, false, true
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc

OUTREACH_STATES = ("draft", "edited", "approved", "sending", "sent", "failed", "rejected")


class MessageTemplate(Base):
    __tablename__ = "message_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    offering_id: Mapped[int | None] = mapped_column(Integer, index=True)  # no FK (see module note)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    subject: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)


class Deck(Base):
    __tablename__ = "decks"

    id: Mapped[int] = mapped_column(primary_key=True)
    offering_id: Mapped[int | None] = mapped_column(Integer, index=True)  # no FK (see module note)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    # Where the PDF lives (services/outreach/storage.py): "local" = <data_dir>/<stored_path>, "gcs" = the
    # SALES0_ATTACHMENTS_BUCKET object <stored_path> (= decks/<sha256>.pdf). The database never holds the bytes.
    storage: Mapped[str] = mapped_column(String(10), nullable=False, default="local", server_default="local")
    # True when the file could not be found (migration q13 / a failed read / push-data): "file missing - upload again".
    content_missing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())


class OutreachMessage(Base):
    __tablename__ = "outreach_messages"
    __table_args__ = (in_check("state", OUTREACH_STATES, "state"), Index("ix_outreach_messages_sent_at", "sent_at"))  # sent_at: Phase 4 metrics

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("team_members.id", ondelete="RESTRICT"), nullable=False, index=True)
    connection_email: Mapped[str] = mapped_column(String(254), nullable=False)
    to_addrs: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    cc_addrs: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    from_addr: Mapped[str] = mapped_column(String(254), nullable=False)
    subject: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    template_id: Mapped[int | None] = mapped_column(Integer)
    deck_id: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(10), nullable=False, default="draft", server_default="draft", index=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    approved_by_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    gmail_draft_id: Mapped[str | None] = mapped_column(String(120))
    gmail_message_id: Mapped[str | None] = mapped_column(String(120))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(120))
    error: Mapped[str | None] = mapped_column(Text)
    send_outcome_unknown: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    retry_decided_by: Mapped[str | None] = mapped_column(String(80))
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="SET NULL"), index=True)
    enquiry_id: Mapped[int | None] = mapped_column(ForeignKey("enquiries.id", ondelete="SET NULL"), index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    brief_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Reply tracking: none | replied | auto_reply | bounced (set by the reply poller's subscriber).
    reply_status: Mapped[str] = mapped_column(String(12), nullable=False, default="none", server_default="none")
    last_reply_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # A follow-up draft points at the message it follows up; a reply stops pending follow-ups.
    follow_up_of_id: Mapped[int | None] = mapped_column(ForeignKey("outreach_messages.id", ondelete="SET NULL"), index=True)


class OutreachEvent(Base):
    """Append-only audit / timeline of an outreach message."""

    __tablename__ = "outreach_events"
    __table_args__ = (Index("ix_outreach_events_message_id_created_at", "message_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("outreach_messages.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    member_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)


class CompanyBrief(Base):
    __tablename__ = "company_briefs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    website: Mapped[str | None] = mapped_column(String(300))
    brief_json: Mapped[str] = mapped_column(Text, nullable=False)
    sources_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, index=True)
    cost_usd_micros: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    enquiry_id: Mapped[int | None] = mapped_column(ForeignKey("enquiries.id", ondelete="SET NULL"), index=True)
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="SET NULL"), index=True)


__all__ = [
    "OUTREACH_STATES",
    "CompanyBrief",
    "Deck",
    "MessageTemplate",
    "OutreachEvent",
    "OutreachMessage",
]
