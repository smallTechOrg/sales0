"""Discovery runs and their per-keyword log rows."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, deferred, mapped_column, relationship

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check
from sales0.db.models.enums import (
    ABORT_REASONS,
    RUN_KEYWORD_STATUSES,
    RUN_MODES,
    RUN_STATUSES,
    RUN_TRIGGERS,
)


def _counter() -> Mapped[int]:
    return mapped_column(Integer, nullable=False, default=0, server_default="0")


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"
    __table_args__ = (
        in_check("trigger", RUN_TRIGGERS, "trigger"),
        in_check("mode", RUN_MODES, "mode"),
        in_check("status", RUN_STATUSES, "status"),
        in_check("abort_reason", ABORT_REASONS, "abort_reason", nullable=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(
        String(20), nullable=False, default="manual", server_default="manual"
    )
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="queued", server_default="queued"
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    keywords_planned: Mapped[int] = _counter()
    keywords_done: Mapped[int] = _counter()
    current_keyword: Mapped[str | None] = mapped_column(String(100))
    current_step: Mapped[str | None] = mapped_column(String(200))
    posts_seen: Mapped[int] = _counter()
    posts_new: Mapped[int] = _counter()
    posts_dupe: Mapped[int] = _counter()
    posts_relevant: Mapped[int] = _counter()
    posts_skipped_ai: Mapped[int] = _counter()
    permalinks_found: Mapped[int] = _counter()
    # q14: genuine posts auto-discarded as a repeat from an existing prospect (beyond the first one in this run)
    posts_repeat_skipped: Mapped[int] = _counter()
    enquiries_triaged: Mapped[int] = _counter()
    triage_failed: Mapped[int] = _counter()
    errors_count: Mapped[int] = _counter()
    # p9: LinkedIn page loads this run used (per-account daily page-load cap)
    page_loads: Mapped[int] = _counter()
    abort_reason: Mapped[str | None] = mapped_column(String(20))
    error_message: Mapped[str | None] = mapped_column(String(500))
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("team_members.id", ondelete="SET NULL")
    )
    # p8: which LinkedIn account ran (cool-down / daily caps are per account). NULL only on rows
    # written before p8 that the migration could not stamp.
    linkedin_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("linkedin_accounts.id", ondelete="SET NULL"), index=True
    )
    # p10 (hosted): which laptop worker claimed the run, when, its last heartbeat, and the Stop request.
    worker_name: Mapped[str | None] = mapped_column(String(80))
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    keywords: Mapped[list[ScrapeRunKeyword]] = relationship(
        back_populates="run",
        order_by="ScrapeRunKeyword.id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ScrapeRunKeyword(Base):
    __tablename__ = "scrape_run_keywords"
    __table_args__ = (in_check("status", RUN_KEYWORD_STATUSES, "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("scrape_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    keyword_id: Mapped[int | None] = mapped_column(
        ForeignKey("keywords.id", ondelete="SET NULL")
    )
    keyword_text: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    posts_seen: Mapped[int] = _counter()
    posts_new: Mapped[int] = _counter()
    posts_dupe: Mapped[int] = _counter()
    posts_relevant: Mapped[int] = _counter()
    posts_skipped_ai: Mapped[int] = _counter()
    permalinks_found: Mapped[int] = _counter()
    scrolls: Mapped[int] = _counter()
    cost_usd_micros: Mapped[int] = _counter()
    errors: Mapped[int] = _counter()
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(String(300))
    # p10 (hosted): highest post-upload batch number applied (a retried upload is acknowledged, not re-applied)
    upload_seq: Mapped[int] = _counter()
    # q21: JSON of ``scraper.layout_sample.capture`` when the keyword ended with a layout warning (deferred: it can be
    # ~120 KB and is only read by the download route)
    layout_sample_json: Mapped[str | None] = deferred(mapped_column(Text, nullable=True))

    run: Mapped[ScrapeRun] = relationship(back_populates="keywords")


class WatchRun(Base):
    """Marks a ``scrape_runs`` row as a prospect-watch run (its per-profile log rows are
    ``scrape_run_keywords`` rows with ``keyword_text`` 'watch:<slug>'). ``scrape_runs.trigger`` has a
    CHECK we cannot widen without rebuilding a table other tables cascade from, hence this marker."""

    __tablename__ = "watch_runs"

    run_id: Mapped[int] = mapped_column(
        ForeignKey("scrape_runs.id", ondelete="CASCADE"), primary_key=True
    )
    origin: Mapped[str] = mapped_column(String(12), nullable=False, default="manual", server_default="manual")
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
