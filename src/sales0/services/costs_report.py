"""Phase 4 cost view and the monthly spend cap. Reads ``llm_calls`` (+ counts of drafts) only.

Spend cap (``SALES0_MONTHLY_SPEND_CAP_USD``, 0 = off), measured over the local calendar month:

* >= ``SALES0_SPEND_CAP_WARN_PCT`` % of the cap -> a warning banner (state ``warn``);
* >= 100 % -> state ``exceeded``: :func:`ensure_within_spend_cap` raises
  :class:`SpendCapReached` before EVERY LLM call (triage, research, drafting, suggestions,
  digest summary). There is no "essential" LLM call. A discovery run keeps what it already
  saved but stops triaging: the remaining enquiries stay ``pending`` (Retry triage once the
  cap is raised or the month rolls over). The scheduler also refuses to start nightly runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc, local_day_bounds_utc, local_month_bounds_utc, local_today
from sales0.config import get_settings
from sales0.db.models import LlmCall, OutreachMessage
from sales0.errors import DomainError
from sales0.services._common import now_or

FEATURES = ("triage", "research", "drafting", "suggest", "reply", "summary")


class SpendCapReached(DomainError):
    """The monthly AI spend cap is used up; the LLM call was NOT made."""

    def __init__(self, message: str) -> None:
        super().__init__("spend_cap_reached", message, 429)
        self.error_type = "spend_cap_reached"


def _fmt(micros: int) -> str:
    value = micros / 1e6
    return f"${value:.4f}" if value < 1 else f"${value:,.2f}"


@dataclass(frozen=True)
class SpendCap:
    cap_micros: int  # 0 = off
    month_micros: int
    state: str  # off | ok | warn | exceeded
    pct: float  # 0 when off

    @property
    def enabled(self) -> bool:
        return self.cap_micros > 0

    @property
    def message(self) -> str | None:
        if self.state == "exceeded":
            return (
                f"Monthly AI spend cap reached ({_fmt(self.month_micros)} of {_fmt(self.cap_micros)}). "
                "AI features are paused until next month or until you raise SALES0_MONTHLY_SPEND_CAP_USD; "
                "new enquiries stay pending."
            )
        if self.state == "warn":
            return (
                f"AI spend is at {self.pct:.0f}% of the monthly cap "
                f"({_fmt(self.month_micros)} of {_fmt(self.cap_micros)})."
            )
        return None


def month_spend_micros(db: Session, now: datetime | None = None) -> int:
    start, end = local_month_bounds_utc(get_settings().tz, now_or(now))
    return int(
        db.scalar(
            select(func.coalesce(func.sum(LlmCall.cost_usd_micros), 0)).where(
                LlmCall.created_at >= start, LlmCall.created_at < end
            )
        )
        or 0
    )


def spend_cap_status(db: Session, now: datetime | None = None) -> SpendCap:
    settings = get_settings()
    cap = int(round(settings.monthly_spend_cap_usd * 1_000_000))
    spent = month_spend_micros(db, now)
    if cap <= 0:
        return SpendCap(0, spent, "off", 0.0)
    pct = spent * 100.0 / cap
    state = "exceeded" if spent >= cap else "warn" if pct >= settings.spend_cap_warn_pct else "ok"
    return SpendCap(cap, spent, state, pct)


def ensure_within_spend_cap(db: Session, now: datetime | None = None) -> None:
    """Raise :class:`SpendCapReached` when the cap is used up. Call before every LLM call.

    Known, accepted limit (audit #18): the check compares *recorded* spend (``llm_calls`` rows, written after each
    call) with the cap; there is no reservation for calls already in flight. Overshoot is therefore bounded by
    the calls that were started before the first over-cap row was written: at most ``SALES0_TRIAGE_CONCURRENCY``
    (default 4) parallel triage calls plus one call per other concurrently running feature (research, drafting,
    suggestion, digest summary, reply classification), i.e. a few cents at Haiku prices, never unbounded."""
    status = spend_cap_status(db, now)
    if status.state == "exceeded":
        raise SpendCapReached(status.message or "Monthly AI spend cap reached.")


# ---------------------------------------------------------------------- report
@dataclass(frozen=True)
class Bucket:
    key: str
    cost_micros: int
    calls: int
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class CostReport:
    today_micros: int = 0
    today_calls: int = 0
    month_micros: int = 0
    month_calls: int = 0
    daily: list[Bucket] = field(default_factory=list)  # newest first, last 30 local days incl. zero days
    monthly: list[Bucket] = field(default_factory=list)  # last 6 local months, newest first
    by_feature: list[Bucket] = field(default_factory=list)  # this month
    by_model: list[Bucket] = field(default_factory=list)  # this month
    triaged_enquiries: int = 0  # distinct enquiries with an ok triage call this month
    cost_per_enquiry_micros: int | None = None  # triage cost / triaged enquiries
    drafts: int = 0  # outreach drafts created this month
    cost_per_draft_micros: int | None = None  # drafting cost / drafts
    errors_month: int = 0
    cap: SpendCap | None = None


def _sum_calls(db: Session, start: datetime, end: datetime) -> tuple[int, int]:
    total, calls = db.execute(
        select(func.coalesce(func.sum(LlmCall.cost_usd_micros), 0), func.count(LlmCall.id)).where(
            LlmCall.created_at >= start, LlmCall.created_at < end
        )
    ).one()
    return int(total or 0), int(calls or 0)


def _month_starts(today: date, n: int) -> list[date]:
    out, y, m = [], today.year, today.month
    for _ in range(n):
        out.append(date(y, m, 1))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def build_report(db: Session, now: datetime | None = None, *, days: int = 30, months: int = 6) -> CostReport:
    settings = get_settings()
    tz = settings.tz
    now = ensure_utc(now_or(now))
    today = local_today(tz, now)
    rep = CostReport()

    rep.today_micros, rep.today_calls = _sum_calls(db, *local_day_bounds_utc(tz, now))
    mstart, mend = local_month_bounds_utc(tz, now)
    rep.month_micros, rep.month_calls = _sum_calls(db, mstart, mend)

    # Daily: one query for the window, bucketed by local date in Python (timezone-correct).
    first_day = today - timedelta(days=days - 1)
    first_start = local_day_bounds_utc(tz, datetime.combine(first_day, datetime.min.time(), tzinfo=tz))[0]
    rows = db.execute(
        select(LlmCall.created_at, LlmCall.cost_usd_micros).where(LlmCall.created_at >= first_start, LlmCall.created_at <= now)
    ).all()
    per_day: dict[date, list[int]] = {}
    for created_at, cost in rows:
        d = ensure_utc(created_at).astimezone(tz).date()
        slot = per_day.setdefault(d, [0, 0])
        slot[0] += int(cost or 0)
        slot[1] += 1
    rep.daily = [
        Bucket(str(d), *per_day.get(d, [0, 0]))
        for d in (today - timedelta(days=i) for i in range(days))
    ]

    for first in _month_starts(today, months):
        ms, me = local_month_bounds_utc(tz, datetime.combine(first, datetime.min.time(), tzinfo=tz))
        cost, calls = _sum_calls(db, ms, me)
        rep.monthly.append(Bucket(first.strftime("%Y-%m"), cost, calls))

    def grouped(col) -> list[Bucket]:  # type: ignore[no-untyped-def]
        q = (
            select(
                col,
                func.coalesce(func.sum(LlmCall.cost_usd_micros), 0),
                func.count(LlmCall.id),
                func.coalesce(func.sum(LlmCall.input_tokens), 0),
                func.coalesce(func.sum(LlmCall.output_tokens), 0),
            )
            .where(LlmCall.created_at >= mstart, LlmCall.created_at < mend)
            .group_by(col)
            .order_by(func.sum(LlmCall.cost_usd_micros).desc())
        )
        return [Bucket(str(k), int(c or 0), int(n or 0), int(i or 0), int(o or 0)) for k, c, n, i, o in db.execute(q).all()]

    rep.by_feature = grouped(LlmCall.feature)
    rep.by_model = grouped(LlmCall.model)
    feature_cost = {b.key: b.cost_micros for b in rep.by_feature}

    rep.triaged_enquiries = int(
        db.scalar(
            select(func.count(distinct(LlmCall.enquiry_id))).where(
                LlmCall.feature == "triage",
                LlmCall.status == "ok",
                LlmCall.enquiry_id.is_not(None),
                LlmCall.created_at >= mstart,
                LlmCall.created_at < mend,
            )
        )
        or 0
    )
    if rep.triaged_enquiries:
        rep.cost_per_enquiry_micros = feature_cost.get("triage", 0) // rep.triaged_enquiries
    rep.drafts = int(
        db.scalar(select(func.count(OutreachMessage.id)).where(OutreachMessage.created_at >= mstart, OutreachMessage.created_at < mend))
        or 0
    )
    if rep.drafts:
        rep.cost_per_draft_micros = feature_cost.get("drafting", 0) // rep.drafts
    rep.errors_month = int(
        db.scalar(
            select(func.count(LlmCall.id)).where(
                LlmCall.status == "error", LlmCall.created_at >= mstart, LlmCall.created_at < mend
            )
        )
        or 0
    )
    rep.cap = spend_cap_status(db, now)
    return rep


__all__ = [
    "FEATURES",
    "Bucket",
    "CostReport",
    "SpendCap",
    "SpendCapReached",
    "build_report",
    "ensure_within_spend_cap",
    "month_spend_micros",
    "spend_cap_status",
]
