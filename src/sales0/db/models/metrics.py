"""Manual weekly metrics (Phase 4): figures the app cannot observe (blogs/social posts published,
people added to the WhatsApp group). One row per (ISO year, ISO week, metric)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, now_utc

MANUAL_METRIC_KEYS: tuple[str, ...] = ("posts_published", "whatsapp_added")


class ManualMetric(Base):
    __tablename__ = "manual_metrics"
    __table_args__ = (
        UniqueConstraint("iso_year", "iso_week", "metric_key", name="uq_manual_metrics_week_metric"),
        CheckConstraint("metric_key IN ('posts_published', 'whatsapp_added')", name="metric_key"),
        CheckConstraint("value >= 0", name="value_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    iso_year: Mapped[int] = mapped_column(Integer, nullable=False)
    iso_week: Mapped[int] = mapped_column(Integer, nullable=False)
    metric_key: Mapped[str] = mapped_column(String(30), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(80))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)


__all__ = ["MANUAL_METRIC_KEYS", "ManualMetric"]
