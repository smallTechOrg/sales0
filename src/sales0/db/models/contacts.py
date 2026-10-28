"""Shared people (contacts), their emails/phones, tags and polymorphic links (Phase 2)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc
from sales0.db.models.enums_crm import CONTACT_ORIGINS, LINK_ENTITY_TYPES, TAG_CATEGORIES


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (in_check("origin", CONTACT_ORIGINS, "origin"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str | None] = mapped_column(String(200), index=True)
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(40))
    linkedin_slug: Mapped[str | None] = mapped_column(String(200), unique=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    title: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    search_text: Mapped[str] = mapped_column(String(700), nullable=False, default="", server_default="")
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    origin: Mapped[str] = mapped_column(String(10), nullable=False, default="manual", server_default="manual")
    actor: Mapped[str] = mapped_column(String(80), nullable=False, default="system", server_default="system")
    actor_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)


class ContactEmail(Base):
    __tablename__ = "contact_emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())


class ContactPhone(Base):
    __tablename__ = "contact_phones"
    __table_args__ = (UniqueConstraint("contact_id", "digits", name="uq_contact_phones_contact_id_digits"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String(40), nullable=False)
    digits: Mapped[str] = mapped_column(String(20), nullable=False, index=True)


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (in_check("category", TAG_CATEGORIES, "category"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    name_key: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    category: Mapped[str] = mapped_column(String(10), nullable=False, default="custom", server_default="custom")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)


class ContactTag(Base):
    __tablename__ = "contact_tags"

    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True, index=True)


class ContactLink(Base):
    __tablename__ = "contact_links"
    __table_args__ = (
        UniqueConstraint("contact_id", "entity_type", "entity_id", name="uq_contact_links_contact_entity"),
        Index("ix_contact_links_entity", "entity_type", "entity_id"),
        in_check("entity_type", LINK_ENTITY_TYPES, "entity_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(12), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)


__all__ = ["Contact", "ContactEmail", "ContactLink", "ContactPhone", "ContactTag", "Tag"]
