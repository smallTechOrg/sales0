"""LinkedIn author identities (Phase 1.4): one row per distinct profile slug."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc
from sales0.db.models.enums import PROFILE_KINDS


class LinkedinProfile(Base):
    __tablename__ = "linkedin_profiles"
    __table_args__ = (in_check("kind", PROFILE_KINDS, "kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    profile_url: Mapped[str] = mapped_column(String(500), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    headline: Mapped[str | None] = mapped_column(String(500))
    first_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=now_utc
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=now_utc
    )
    posts_seen: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    relevant_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # NULL = never tracked; Phase 2 sets true (and adds prospect_id) when a prospect is created.
    tracked: Mapped[bool | None] = mapped_column(Boolean)
    # Prospect watch (columns on this table, not a separate watch_state table: one row per profile,
    # no joins on the hot list query, and ADD COLUMN needs no SQLite table rebuild).
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    watch_checks: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    watch_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    watch_last_status: Mapped[str | None] = mapped_column(String(20))
    # Paused until (a far-future value = paused by the user); NULL = watchable.
    watch_disabled_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
