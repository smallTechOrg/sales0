"""Triage decisions: the append-only learning-loop signal."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc
from sales0.db.models.enums import DECISIONS, VERDICTS


class TriageDecision(Base):
    __tablename__ = "triage_decisions"
    __table_args__ = (
        in_check("decision", DECISIONS, "decision"),
        in_check("ai_verdict", VERDICTS, "ai_verdict", nullable=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    enquiry_id: Mapped[int] = mapped_column(
        ForeignKey("enquiries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(10), nullable=False)
    ai_verdict: Mapped[str | None] = mapped_column(String(10))
    is_override: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200))
    field_overrides: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    decided_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=now_utc, index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
