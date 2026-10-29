"""The grouped offerings catalogue that triage matches against."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, true
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, name_key, now_utc


class OfferingGroup(Base):
    __tablename__ = "offering_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    name_key: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=now_utc
    )

    offerings: Mapped[list[Offering]] = relationship(
        back_populates="group", order_by="Offering.sort_order", passive_deletes="all"
    )

    @validates("name")
    def _set_name_key(self, _key: str, value: str) -> str:
        value = value.strip()
        self.name_key = name_key(value)
        return value


class Offering(Base):
    __tablename__ = "offerings"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("offering_groups.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    name_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", server_default=""
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=now_utc, onupdate=now_utc
    )

    group: Mapped[OfferingGroup] = relationship(back_populates="offerings")

    @validates("name")
    def _set_name_key(self, _key: str, value: str) -> str:
        value = value.strip()
        self.name_key = name_key(value)
        return value
