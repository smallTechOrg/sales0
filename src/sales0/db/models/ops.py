"""Phase 4 operations tables: scheduler state, stored digests, AI suggestions.

No foreign keys to CRM tables (they arrive in Phase 2): ``digests.sections_json`` is a
registry-built snapshot and suggestions reference keywords by plain ``keyword_id``.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, Integer, String, Text, true
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc

SCHEDULE_STATUSES = ("ok", "skipped", "failed", "running", "missed")
SUGGESTION_KINDS = ("new_keyword", "pause_keyword")
SUGGESTION_STATUSES = ("pending", "accepted", "dismissed")


class ScheduledJob(Base):
    """One row per named job: enable flag, next run, last result and job-private state."""

    __tablename__ = "scheduled_jobs"
    __table_args__ = (in_check("last_status", SCHEDULE_STATUSES, "last_status", nullable=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_status: Mapped[str | None] = mapped_column(String(10))
    last_detail: Mapped[str | None] = mapped_column(String(500))
    last_run_id: Mapped[int | None] = mapped_column(Integer)  # scrape_runs.id for discovery_run (no FK)
    runs_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    state_json: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)


class Digest(Base):
    """The stored daily digest (one per local date; rebuilding replaces it)."""

    __tablename__ = "digests"

    id: Mapped[int] = mapped_column(primary_key=True)
    digest_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    trigger: Mapped[str] = mapped_column(String(10), nullable=False, default="manual", server_default="manual")
    sections_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_text: Mapped[str | None] = mapped_column(Text)
    summary_cost_usd_micros: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    gmail_draft_id: Mapped[str | None] = mapped_column(String(120))
    gmail_draft_member_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, index=True)


class Suggestion(Base):
    """A proactive suggestion from the suggestions agent (keyword to add / keyword to pause)."""

    __tablename__ = "suggestions"
    __table_args__ = (
        in_check("kind", SUGGESTION_KINDS, "kind"),
        in_check("status", SUGGESTION_STATUSES, "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    text: Mapped[str] = mapped_column(String(100), nullable=False)  # phrase, or the keyword text to pause
    keyword_id: Mapped[int | None] = mapped_column(Integer)  # pause_keyword: keywords.id (no FK)
    dedupe_key: Mapped[str] = mapped_column(String(140), nullable=False, unique=True)  # "<kind>:<lower text>"
    rationale: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default="")
    evidence_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="pending", server_default="pending", index=True)
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    decided_by_member_id: Mapped[int | None] = mapped_column(Integer)


__all__ = [
    "SCHEDULE_STATUSES",
    "SUGGESTION_KINDS",
    "SUGGESTION_STATUSES",
    "Digest",
    "ScheduledJob",
    "Suggestion",
]
