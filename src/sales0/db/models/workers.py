"""Laptop workers that poll the hosted app (``/api/worker/*``)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, now_utc


class Worker(Base):
    __tablename__ = "workers"
    __table_args__ = (UniqueConstraint("name", name="uq_workers_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False, default="", server_default="")
    host: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="")
    protocol: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    linkedin_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown", server_default="unknown")
    linkedin_detail: Mapped[str | None] = mapped_column(String(300))
    last_run_id: Mapped[int | None] = mapped_column(Integer)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
