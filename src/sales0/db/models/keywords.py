"""LinkedIn search keywords."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, true
from sqlalchemy.orm import Mapped, mapped_column, validates

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, collapse_ws, now_utc


class Keyword(Base):
    __tablename__ = "keywords"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(String(100), nullable=False)
    text_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    @validates("text")
    def _normalise_text(self, _key: str, value: str) -> str:
        """Trim + collapse inner whitespace (original case kept); text_key = lower(text)."""
        value = collapse_ws(value)
        self.text_key = value.lower()
        return value
