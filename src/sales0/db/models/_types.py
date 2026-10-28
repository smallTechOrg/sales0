"""Shared column types and helpers for the ORM models."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from sales0.clock import ensure_utc, utcnow


class UTCDateTime(TypeDecorator[datetime]):
    """``DateTime(timezone=True)`` that always writes UTC and always reads aware UTC.

    SQLite stores no offset and returns naive values; this type normalises aware inputs to
    UTC on bind (naive inputs are treated as UTC) and re-attaches UTC on load, so filters
    with aware bounds compare correctly on SQLite and Postgres alike.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        value = ensure_utc(value)
        if dialect.name == "sqlite":
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return ensure_utc(value)


def now_utc() -> datetime:
    """Column default (delegates to the clock seam)."""
    return utcnow()


def in_check(
    column: str, values: tuple[str, ...], name: str, *, nullable: bool = False
) -> CheckConstraint:
    """A named CHECK constraint restricting an enum-like string column to ``values``."""
    allowed = ", ".join(f"'{v}'" for v in values)
    expr = f"{column} IN ({allowed})"
    if nullable:
        expr = f"{column} IS NULL OR {expr}"
    return CheckConstraint(expr, name=name)


_WS = re.compile(r"\s+")


def collapse_ws(text: str) -> str:
    """Trim and collapse inner whitespace to single spaces."""
    return _WS.sub(" ", text).strip()


def name_key(name: str) -> str:
    """``lower(trim(name))`` — the case-insensitive uniqueness key."""
    return name.strip().lower()


__all__ = ["UTC", "UTCDateTime", "collapse_ws", "in_check", "name_key", "now_utc"]
