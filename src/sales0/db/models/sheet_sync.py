"""Sheet-sync bookkeeping: one row (id=1) with the last run and its per-tab report JSON."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check

SHEET_SYNC_STATE_ID = 1
SHEET_SYNC_STATUSES = ("ok", "partial", "failed")


class SheetSyncState(Base):
    __tablename__ = "sheet_sync_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_sheet_sync_state_singleton"),
        in_check("last_status", SHEET_SYNC_STATUSES, "last_status", nullable=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True, default=SHEET_SYNC_STATE_ID)
    last_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_status: Mapped[str | None] = mapped_column(String(10))
    last_error: Mapped[str | None] = mapped_column(String(500))
    last_dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    last_duration_s: Mapped[float | None] = mapped_column(Float)
    last_report_json: Mapped[str | None] = mapped_column(Text)  # per-tab counts + issue codes
    runs_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
