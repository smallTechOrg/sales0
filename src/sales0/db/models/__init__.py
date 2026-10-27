"""ORM models (Alembic autogenerate target).

Scaffold placeholder: exports ``AppMeta`` only. Phase-1 slice A replaces this module with
one module per aggregate (see spec/data.md) and re-exports every model here.
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base


class AppMeta(Base):
    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), nullable=False)


__all__ = ["AppMeta"]
