"""Sheet-sync bookkeeping (Phase 2): per-tab source rows and the metrics baseline."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc
from sales0.db.models.enums_crm import METRIC_SETS, SHEET_ENTITIES, SHEET_TARGET_TYPES


class SheetSource(Base):
    __tablename__ = "sheet_sources"
    __table_args__ = (
        UniqueConstraint("entity", "key", "sheet_id", "gid", name="uq_sheet_sources_entity_key_sheet_gid"),
        Index("ix_sheet_sources_sheet_gid", "sheet_id", "gid"),
        Index("ix_sheet_sources_target", "target_type", "target_id"),
        in_check("entity", SHEET_ENTITIES, "entity"),
        in_check("target_type", SHEET_TARGET_TYPES, "target_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity: Mapped[str] = mapped_column(String(12), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    target_type: Mapped[str] = mapped_column(String(14), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    sheet_id: Mapped[str] = mapped_column(String(64), nullable=False)
    gid: Mapped[str] = mapped_column(String(20), nullable=False)
    tab_name: Mapped[str] = mapped_column(String(60), nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    row_hash: Mapped[str] = mapped_column(String(40), nullable=False)
    fields: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)


class SheetMetric(Base):
    __tablename__ = "sheet_metrics"
    __table_args__ = (in_check("metric_set", METRIC_SETS, "metric_set"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    metric_set: Mapped[str] = mapped_column(String(10), nullable=False)
    group_label: Mapped[str | None] = mapped_column(String(200))
    metric: Mapped[str] = mapped_column(String(200), nullable=False)
    metric_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    weekly_target: Mapped[float | None] = mapped_column(Float)
    weekly_target_raw: Mapped[str | None] = mapped_column(String(60))
    sheet_removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sheet_fields: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)


class SheetMetricWeek(Base):
    __tablename__ = "sheet_metric_weeks"
    __table_args__ = (
        UniqueConstraint("metric_id", "week_no", "occurrence", name="uq_sheet_metric_weeks_metric_week_occ"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("sheet_metrics.id", ondelete="CASCADE"), nullable=False, index=True)
    week_no: Mapped[int] = mapped_column(Integer, nullable=False)
    occurrence: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    label: Mapped[str] = mapped_column(String(20), nullable=False, default="", server_default="")
    raw: Mapped[str] = mapped_column(String(60), nullable=False, default="", server_default="")
    value: Mapped[float | None] = mapped_column(Float)


__all__ = ["SheetMetric", "SheetMetricWeek", "SheetSource"]
