"""One row per distinct post sighted, whatever the AI verdict (Phase 1.4)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Date, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc
from sales0.db.models.enums import SEEN_TRIAGE_STATUSES, VERDICTS


class SeenPost(Base):
    __tablename__ = "seen_posts"
    __table_args__ = (
        Index("ix_seen_posts_verdict_first_seen", "verdict", "first_seen_at"),
        Index("ix_seen_posts_first_seen_at", "first_seen_at"),  # Phase 4 metrics: weekly scanned counts
        in_check("triage_status", SEEN_TRIAGE_STATUSES, "triage_status"),
        in_check("verdict", VERDICTS, "verdict", nullable=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    text_prefix: Mapped[str] = mapped_column(
        String(300), nullable=False, default="", server_default=""
    )
    source_ref: Mapped[str | None] = mapped_column(String(200), unique=True)
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("linkedin_profiles.id", ondelete="SET NULL"), index=True
    )
    author_name: Mapped[str | None] = mapped_column(String(200))
    author_headline: Mapped[str | None] = mapped_column(String(500))
    keyword_text: Mapped[str] = mapped_column(String(100), nullable=False)
    first_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("scrape_runs.id", ondelete="SET NULL")
    )
    triage_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="pending", server_default="pending"
    )
    triage_error: Mapped[str | None] = mapped_column(String(300))
    verdict: Mapped[str | None] = mapped_column(String(10))
    reason: Mapped[str | None] = mapped_column(String(160))
    poster_type: Mapped[str | None] = mapped_column(String(40))
    ai_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    snippet: Mapped[str] = mapped_column(
        String(240), nullable=False, default="", server_default=""
    )
    raw_text: Mapped[str | None] = mapped_column(Text)
    raw_purge_after: Mapped[datetime | None] = mapped_column(UTCDateTime())
    raw_purged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    post_url: Mapped[str | None] = mapped_column(String(500))
    posted_relative: Mapped[str | None] = mapped_column(String(30))
    # p11 maximum raw capture; NULL on rows scraped before it (never captured).
    posted_date: Mapped[date | None] = mapped_column(Date)
    truncated: Mapped[bool | None] = mapped_column(Boolean)
    link_kind: Mapped[str | None] = mapped_column(String(10))
    search_url: Mapped[str | None] = mapped_column(String(2000))
    raw_card: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    enquiry_id: Mapped[int | None] = mapped_column(
        ForeignKey("enquiries.id", ondelete="SET NULL"), index=True
    )
    rescued_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    first_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=now_utc
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=now_utc
    )
    times_seen: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
