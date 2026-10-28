"""LinkedIn accounts (p8) and the legacy session singleton (id = 1, seeded by migration 0002).

``linkedin_session`` is LEGACY: since p8 nothing reads or writes it (the p8 migration copied it into
the default ``linkedin_accounts`` row). It stays in the schema so old data is never dropped.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc
from sales0.db.models.enums import LINKEDIN_ACCOUNT_STATUSES, LINKEDIN_BACKENDS, LINKEDIN_SESSION_STATUSES

LINKEDIN_SESSION_ID = 1


class LinkedInSession(Base):
    __tablename__ = "linkedin_session"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        in_check("status", LINKEDIN_SESSION_STATUSES, "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, default=LINKEDIN_SESSION_ID)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="not_connected",
        server_default="not_connected",
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_connected_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_successful_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc
    )


class LinkedInAccount(Base):
    """One LinkedIn login sales0 scrapes with. All safety / cool-down / daily-counter state is per row."""

    __tablename__ = "linkedin_accounts"
    __table_args__ = (
        in_check("status", LINKEDIN_ACCOUNT_STATUSES, "status"),
        in_check("backend", LINKEDIN_BACKENDS, "backend"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    # team_members.id of the owner; plain column (no FK) so team tables can change independently.
    owner_member_id: Mapped[int | None] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="not_connected", server_default="not_connected"
    )
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    backend: Mapped[str] = mapped_column(String(10), nullable=False, default="folder", server_default="folder")
    profile_dirname: Mapped[str | None] = mapped_column(String(120))  # folder backend
    session_state: Mapped[str | None] = mapped_column(Text)  # db backend: ENCRYPTED storage_state JSON
    # pinned browser identity (NULL = browser default)
    proxy_url: Mapped[str | None] = mapped_column(String(300))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    locale: Mapped[str | None] = mapped_column(String(20))
    timezone: Mapped[str | None] = mapped_column(String(60))
    viewport: Mapped[str | None] = mapped_column(String(20))  # "1280x860"
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_connected_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_successful_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(String(300))
    # safety state: the latest LinkedIn stop of THIS account and the cool-down it caused.
    # ``cooldown_until`` mirrors what services.runs.safety_cooldown_until derives from this
    # account's aborted runs (the runs are authoritative; this column is for display / audit).
    safety_state: Mapped[str | None] = mapped_column(String(20))  # logout|checkpoint|captcha|rate_limited
    last_stop_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_stop_reason: Mapped[str | None] = mapped_column(String(300))
    cooldown_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # daily counters (snapshot of this account's runs for ``counter_day``, local date ISO)
    counter_day: Mapped[str | None] = mapped_column(String(10))
    runs_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    posts_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc
    )
