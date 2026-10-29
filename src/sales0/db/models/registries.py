"""Competitors, channel groups and import batches (Phase 2)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, true
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc
from sales0.db.models.enums_crm import IMPORT_KINDS, KEEP_LEVELS, MENTION_ENTITY_TYPES, MENTION_SOURCES


class Competitor(Base):
    __tablename__ = "competitors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    website: Mapped[str | None] = mapped_column(String(300))
    website_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    approx_clients: Mapped[int | None] = mapped_column(Integer)
    client_names: Mapped[str | None] = mapped_column(Text)
    contact: Mapped[str | None] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(254))
    emails: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True))
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))
    # sheet columns
    sheet_status: Mapped[str | None] = mapped_column(String(200))
    sheet_notes: Mapped[str | None] = mapped_column(Text)
    sheet_removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sheet_fields: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)


class CompetitorMention(Base):
    __tablename__ = "competitor_mentions"
    __table_args__ = (
        UniqueConstraint("competitor_id", "entity_type", "entity_id", name="uq_competitor_mentions_entity"),
        Index("ix_competitor_mentions_entity", "entity_type", "entity_id"),
        in_check("entity_type", MENTION_ENTITY_TYPES, "entity_type"),
        in_check("source", MENTION_SOURCES, "source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id", ondelete="CASCADE"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(10), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(5), nullable=False, default="auto", server_default="auto")
    snippet: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)


class ChannelGroup(Base):
    __tablename__ = "channel_groups"
    __table_args__ = (
        Index("ix_channel_groups_platform_name", "platform", "name_key"),
        in_check("keep_level", KEEP_LEVELS, "keep_level", nullable=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_key: Mapped[str] = mapped_column(String(200), nullable=False)
    platform: Mapped[str] = mapped_column(String(20), nullable=False, default="whatsapp", server_default="whatsapp")
    link: Mapped[str | None] = mapped_column(String(500))
    link_key: Mapped[str | None] = mapped_column(String(500), unique=True)
    details: Mapped[str | None] = mapped_column(String(200))
    keep_level: Mapped[str | None] = mapped_column(String(10))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    notes: Mapped[str | None] = mapped_column(Text)
    sheet_status: Mapped[str | None] = mapped_column(String(200))
    sheet_notes: Mapped[str | None] = mapped_column(Text)
    sheet_removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sheet_fields: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)


class ImportBatch(Base):
    __tablename__ = "import_batches"
    __table_args__ = (in_check("kind", IMPORT_KINDS, "kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    rows_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    rows_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    rows_merged: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    rows_unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    rows_error: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    errors: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON(none_as_null=True))
    actor: Mapped[str] = mapped_column(String(80), nullable=False, default="import", server_default="import")
    actor_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)


__all__ = ["ChannelGroup", "Competitor", "CompetitorMention", "ImportBatch"]
