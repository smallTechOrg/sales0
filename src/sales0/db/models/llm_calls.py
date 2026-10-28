"""One row per Anthropic request (including repair retries): tokens, cost, latency."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc
from sales0.db.models.enums import LLM_CALL_STATUSES, LLM_FEATURES


class LlmCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (
        in_check("feature", LLM_FEATURES, "feature"),
        in_check("status", LLM_CALL_STATUSES, "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    feature: Mapped[str] = mapped_column(String(20), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    enquiry_id: Mapped[int | None] = mapped_column(
        ForeignKey("enquiries.id", ondelete="SET NULL"), index=True
    )
    seen_post_id: Mapped[int | None] = mapped_column(
        ForeignKey("seen_posts.id", ondelete="SET NULL"), index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("scrape_runs.id", ondelete="SET NULL"), index=True
    )
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cost_usd_micros: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    fewshot_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(100))
    request_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=now_utc, index=True
    )
