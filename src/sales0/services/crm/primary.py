"""Which of a company's prospect rows is THE one company-level actions and the company's priority use.

One definition, as SQL ordering, so ``lifecycle.primary_prospect`` (one company) and the boards (a correlated
subquery per row) agree. The open one furthest along (then the oldest); else the newest converted one (it has the
lead); else the most recently closed one."""

from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from sales0.db.models import Prospect
from sales0.db.models.enums_crm import PROSPECT_STAGES


def primary_order(p: Any = Prospect) -> tuple[ColumnElement, ...]:  # type: ignore[type-arg]
    """ORDER BY of ``p`` (``Prospect`` or an alias of it): the primary prospect first."""
    rank = case(*((p.stage == s, i) for i, s in enumerate(PROSPECT_STAGES)), else_=0)
    is_open, is_converted = p.status == "open", p.status == "converted"
    closed = ~p.status.in_(("open", "converted"))
    return (
        case((is_open, 0), (is_converted, 1), else_=2),
        case((is_open, -rank), else_=0),
        # Only differs inside the "closed" bucket (NULL for every row of the other two, so NULL ordering never matters).
        case((closed, func.coalesce(p.closed_at, p.updated_at)), else_=None).desc(),
        case((is_open, p.id), else_=-p.id),
    )


def company_value(company_id: ColumnElement, column: str) -> ColumnElement:  # type: ignore[type-arg]
    """Column ``column`` of the company's primary prospect, as a correlated scalar subquery (NULL: no company or no
    prospect). The inner prospect table is an alias, so this also works inside a query over prospects."""
    inner = aliased(Prospect)
    return (select(getattr(inner, column)).where(inner.company_id == company_id).order_by(*primary_order(inner)).limit(1)
            .scalar_subquery())


__all__ = ["company_value", "primary_order"]
