"""AI cost totals for the header badge (today / this month in SALES0_TIMEZONE)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.clock import local_day_bounds_utc, local_month_bounds_utc
from sales0.config import get_settings
from sales0.db.models import LlmCall
from sales0.services._common import now_or
from sales0.services.views import CostSummary


def _totals(db: Session, start: datetime, end: datetime) -> tuple[int, int]:
    total, calls = db.execute(
        select(func.coalesce(func.sum(LlmCall.cost_usd_micros), 0), func.count(LlmCall.id)).where(
            LlmCall.created_at >= start, LlmCall.created_at < end
        )
    ).one()
    return int(total or 0), int(calls or 0)


def cost_summary(db: Session, now: datetime | None = None) -> CostSummary:
    tz = get_settings().tz
    now = now_or(now)
    today = _totals(db, *local_day_bounds_utc(tz, now))
    month = _totals(db, *local_month_bounds_utc(tz, now))
    return CostSummary(
        today_usd_micros=today[0], month_usd_micros=month[0], today_calls=today[1], month_calls=month[1]
    )


__all__ = ["cost_summary"]
