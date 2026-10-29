"""Enquiries (one per real-world post, after dedupe), their sources and keyword hits."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc
from sales0.db.models.enums import (
    ENGAGEMENT_MODES,
    ENQUIRY_SOURCES,
    ENQUIRY_STAGES,
    PRIORITIES,
    REVIEW_STATUSES,
    TRIAGE_STATUSES,
    VERDICTS,
)


class Enquiry(Base):
    __tablename__ = "enquiries"
    __table_args__ = (
        Index(
            "ix_enquiries_inbox",
            "review_status",
            "ai_verdict",
            "priority",
            "date_added",
        ),
        in_check("source", ENQUIRY_SOURCES, "source"),
        in_check("review_status", REVIEW_STATUSES, "review_status"),
        in_check("stage", ENQUIRY_STAGES, "stage", nullable=True),
        in_check("triage_status", TRIAGE_STATUSES, "triage_status"),
        in_check("ai_verdict", VERDICTS, "ai_verdict", nullable=True),
        in_check("priority", PRIORITIES, "priority", nullable=True),
        in_check("mode", ENGAGEMENT_MODES, "mode", nullable=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="linkedin_post",
        server_default="linkedin_post",
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    author_name: Mapped[str | None] = mapped_column(String(200))
    author_headline: Mapped[str | None] = mapped_column(String(500))
    author_profile_url: Mapped[str | None] = mapped_column(String(1000))
    source_url: Mapped[str | None] = mapped_column(String(1000))
    date_posted: Mapped[date | None] = mapped_column(Date)
    date_added: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=now_utc, index=True
    )

    posted_relative: Mapped[str | None] = mapped_column(String(30))
    author_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("linkedin_profiles.id", ondelete="SET NULL")
    )
    seen_post_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "seen_posts.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_enquiries_seen_post_id",
        )
    )

    # Phase 2 (CRM links; added by plain ALTER TABLE ADD COLUMN, enquiries is FK-referenced).
    prospect_id: Mapped[int | None] = mapped_column(
        ForeignKey("prospects.id", ondelete="SET NULL", use_alter=True, name="fk_enquiries_prospect_id"), index=True
    )
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"), index=True)
    origin: Mapped[str] = mapped_column(String(10), nullable=False, default="discovery", server_default="discovery")
    sheet_status: Mapped[str | None] = mapped_column(String(200))
    sheet_notes: Mapped[str | None] = mapped_column(Text)
    sheet_removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sheet_fields: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # LinkedIn activity id (digits) parsed from the post link / urn; indexed lookup instead of LIKE '%id%' scans.
    activity_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # q14 (inbox feedback A1): the EXISTING pipeline entity this enquiry matched when it was created (plain ids, no FK:
    # a merged/deleted prospect simply stops matching on read), how it matched, and the scrape run it was checked in.
    pipeline_prospect_id: Mapped[int | None] = mapped_column(Integer)
    pipeline_company_id: Mapped[int | None] = mapped_column(Integer)
    pipeline_match: Mapped[str | None] = mapped_column(String(20))
    pipeline_run_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # q14: who discarded it when not the team: 'auto' = a system rule (repeat from an existing prospect). Such
    # discards have no triage_decisions row, so they never feed the few-shot examples.
    discard_source: Mapped[str | None] = mapped_column(String(10))

    review_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending_review",
        server_default="pending_review",
    )
    stage: Mapped[str | None] = mapped_column(String(30))

    triage_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="pending", server_default="pending"
    )
    triage_error: Mapped[str | None] = mapped_column(String(300))
    triaged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    prompt_version: Mapped[str | None] = mapped_column(String(40))

    # Immutable AI output.
    ai_verdict: Mapped[str | None] = mapped_column(String(10))
    ai_reason: Mapped[str | None] = mapped_column(String(160))
    ai_poster_type: Mapped[str | None] = mapped_column(String(40))
    ai_opportunity_type: Mapped[str | None] = mapped_column(String(40))
    ai_priority: Mapped[str | None] = mapped_column(String(10))
    ai_offering_id: Mapped[int | None] = mapped_column(
        ForeignKey("offerings.id", ondelete="RESTRICT")
    )
    ai_extraction: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    # Team-editable values (initialised from the AI output).
    requirement_summary: Mapped[str | None] = mapped_column(String(140))
    priority: Mapped[str | None] = mapped_column(String(10))
    offering_id: Mapped[int | None] = mapped_column(
        ForeignKey("offerings.id", ondelete="RESTRICT")
    )
    company: Mapped[str | None] = mapped_column(String(200), index=True)
    contact_person: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(50))
    city: Mapped[str | None] = mapped_column(String(100))
    mode: Mapped[str | None] = mapped_column(String(10))
    dates_mentioned: Mapped[str | None] = mapped_column(String(300))
    topic: Mapped[str | None] = mapped_column(String(300))
    discard_reason: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    update_note: Mapped[str | None] = mapped_column(Text)
    date_email_sent: Mapped[date | None] = mapped_column(Date)
    date_proposal_sent: Mapped[date | None] = mapped_column(Date)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("team_members.id", ondelete="SET NULL")
    )

    sources: Mapped[list[EnquirySource]] = relationship(
        back_populates="enquiry",
        order_by="EnquirySource.id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    keyword_hits: Mapped[list[EnquiryKeywordHit]] = relationship(
        back_populates="enquiry",
        order_by="EnquiryKeywordHit.id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class EnquirySource(Base):
    __tablename__ = "enquiry_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    enquiry_id: Mapped[int] = mapped_column(
        ForeignKey("enquiries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_ref: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    first_seen_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("scrape_runs.id", ondelete="SET NULL")
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=now_utc
    )

    enquiry: Mapped[Enquiry] = relationship(back_populates="sources")


class EnquiryKeywordHit(Base):
    __tablename__ = "enquiry_keyword_hits"
    __table_args__ = (UniqueConstraint("enquiry_id", "run_id", "keyword_text"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    enquiry_id: Mapped[int] = mapped_column(
        ForeignKey("enquiries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("scrape_runs.id", ondelete="SET NULL")
    )
    keyword_id: Mapped[int | None] = mapped_column(
        ForeignKey("keywords.id", ondelete="SET NULL")
    )
    keyword_text: Mapped[str] = mapped_column(String(100), nullable=False)
    seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=now_utc
    )

    enquiry: Mapped[Enquiry] = relationship(back_populates="keyword_hits")
