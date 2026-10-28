"""Discovery runs and their per-keyword log rows."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
    enquiries_triaged: Mapped[int] = _counter()
    triage_failed: Mapped[int] = _counter()
    errors_count: Mapped[int] = _counter()
    abort_reason: Mapped[str | None] = mapped_column(String(20))
    error_message: Mapped[str | None] = mapped_column(String(500))
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

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

    run: Mapped[ScrapeRun] = relationship(back_populates="keywords")
