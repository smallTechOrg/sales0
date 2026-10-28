"""Users: created empty in Phase 1 (rows arrive in Phase 2/5); FK target for actor_user_id."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, now_utc


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    email: Mapped[str | None] = mapped_column(String(254), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=now_utc
    )
