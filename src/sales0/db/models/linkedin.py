"""The LinkedIn session singleton (id = 1, seeded by migration 0002)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc
from sales0.db.models.enums import LINKEDIN_SESSION_STATUSES

LINKEDIN_SESSION_ID = 1


class LinkedInSession(Base):
    __tablename__ = "linkedin_session"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        in_check("status", LINKEDIN_SESSION_STATUSES, "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, default=LINKEDIN_SESSION_ID)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="not_connected", server_default="not_connected"
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_connected_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_successful_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc)
