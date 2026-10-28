"""Persistent inputs of the dashboard: manual weekly figures, target overrides, the sheet baseline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import AppMeta, ManualMetric, SheetMetric, SheetMetricWeek
from sales0.db.models.metrics import MANUAL_METRIC_KEYS
from sales0.errors import DomainError
from sales0.services._common import now_or

# --------------------------------------------------------------------------- vocabulary
WEEKLY_ROWS: tuple[tuple[str, str, str], ...] = (
    # key, label (the Weekly sheet's wording), kind
    ("enquiries_added", "Number of Enquiries added", "computed"),
    ("enquiries_contacted", "Number of Enquiries contacted", "computed"),
    ("meetings_proposals", "Number of Meetings or Proposals with pricing", "computed"),
    ("old_prospects", "No. of old prospects lookup", "computed"),
    ("posts_published", "No. of blogs or social posts published", "manual"),
    ("whatsapp_added", "No. of people added to the whatsapp group", "manual"),
)
WEEKLY_MATCH: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("enquiries_added", ("enquir", "added")),
    ("enquiries_contacted", ("enquir", "contacted")),
    ("meetings_proposals", ("meeting",)),
    ("old_prospects", ("old", "prospect")),
    ("posts_published", ("blog",)),
    ("whatsapp_added", ("whatsapp", "group")),
)


@dataclass(frozen=True)
class SourceDef:
    key: str
    label: str
    counter: str | None  # compute.py counter; None = not measurable
    default_target: float | None
    match: tuple[str, ...] | None  # tokens in the Secondary tab's metric name
    enabled: bool = True
    note: str = ""


SOURCES: tuple[SourceDef, ...] = (
    SourceDef("linkedin_keyword", "LinkedIn keyword scraping", "src_linkedin_keyword", 15, ("linkedin", "scrap"),
              note="relevant enquiries found by keyword runs"),
    SourceDef("profile_watch", "LinkedIn profile watch", "profile_watch", None, None, note="tracked-profile changes"),
    SourceDef("manual_paste", "Manual / paste", "src_manual", None, None, note="enquiries pasted or added by hand"),
    SourceDef("whatsapp", "WhatsApp group scraping", None, 5, ("whatsapp",), enabled=False, note="not enabled"),
    SourceDef("linkedin_outreach", "LinkedIn outreach", "linkedin_outreach", 5, ("linkedin", "outreach"),
              note="connection requests / messages recorded on prospects"),
    SourceDef("messages_sent", "Messages sent", "messages_sent", 25, ("messages", "sent"),
              note="outreach emails + manual 'contacted' records"),
    SourceDef("first_follow_up", "First follow-ups", "first_follow_up", 20, ("first", "follow"),
              note="prospects moved to Follow-up 1"),
)
SOURCE_BY_KEY = {s.key: s for s in SOURCES}
TARGET_PREFIX = "metrics_target."


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _matches(name: str, tokens: tuple[str, ...]) -> bool:
    n = _norm(name)
    return all(t in n for t in tokens)


# --------------------------------------------------------------------------- manual figures
def manual_values(db: Session) -> dict[tuple[str, int, int], int]:
    return {(m.metric_key, m.iso_year, m.iso_week): m.value for m in db.scalars(select(ManualMetric))}


def set_manual(db: Session, metric_key: str, iso_year: int, iso_week: int, value: int | None, *, actor: str = "team") -> None:
    if metric_key not in MANUAL_METRIC_KEYS:
        raise DomainError("metric_invalid", "Choose posts published or people added to the WhatsApp group.", 400)
    try:
        date.fromisocalendar(iso_year, iso_week, 1)
    except ValueError:
        raise DomainError("week_invalid", "That is not a valid ISO week.", 400) from None
    row = db.scalar(
        select(ManualMetric).where(
            ManualMetric.metric_key == metric_key, ManualMetric.iso_year == iso_year, ManualMetric.iso_week == iso_week
        )
    )
    if value is None:
        if row is not None:
            db.delete(row)
        db.flush()
        return
    if value < 0 or value > 100_000:
        raise DomainError("value_invalid", "Enter a whole number from 0 to 100000.", 400)
    if row is None:
        db.add(ManualMetric(metric_key=metric_key, iso_year=iso_year, iso_week=iso_week, value=value, updated_by=actor[:80]))
    else:
        row.value, row.updated_by, row.updated_at = value, actor[:80], now_or(None)
    db.flush()


# --------------------------------------------------------------------------- target overrides
def target_overrides(db: Session) -> dict[str, float]:
    out: dict[str, float] = {}
    for m in db.scalars(select(AppMeta).where(AppMeta.key.like(TARGET_PREFIX + "%"))):
        try:
            out[m.key[len(TARGET_PREFIX):]] = float(m.value)
        except ValueError:
            continue
    return out


def set_target_override(db: Session, source_key: str, value: float | None) -> None:
    src = SOURCE_BY_KEY.get(source_key)
    if src is None or not src.enabled:
        raise DomainError("source_invalid", "That source has no target.", 400)
    key = TARGET_PREFIX + source_key
    row = db.get(AppMeta, key)
    if value is None:
        if row is not None:
            db.delete(row)
        db.flush()
        return
    if value < 0 or value > 100_000:
        raise DomainError("value_invalid", "Enter a target from 0 to 100000.", 400)
    text = f"{value:g}"
    if row is None:
        db.add(AppMeta(key=key, value=text))
    else:
        row.value = text
    db.flush()


# --------------------------------------------------------------------------- sheet baseline
@dataclass
class SheetBaseline:
    weekly: dict[tuple[str, int, int], float]  # (row key, iso year, iso week) -> value
    secondary: dict[tuple[str, int, int], float]  # (source key, iso year, iso week) -> actual
    targets: dict[str, float]  # source key -> weekly target from the sheet


def _latest_year(today: date, week_no: int) -> int:
    """Most recent ISO year whose week ``week_no`` has already started (nearest past occurrence)."""
    for year in (today.isocalendar().year, today.isocalendar().year - 1, today.isocalendar().year - 2):
        try:
            if date.fromisocalendar(year, week_no, 1) <= today:
                return year
        except ValueError:
            continue
    return today.isocalendar().year - 1


def sheet_baseline(db: Session, today: date) -> SheetBaseline:
    base = SheetBaseline({}, {}, {})
    metrics = db.scalars(select(SheetMetric).where(SheetMetric.sheet_removed_at.is_(None)).order_by(SheetMetric.id)).all()
    for m in metrics:
        if m.metric_set == "weekly":
            key = next((k for k, tokens in WEEKLY_MATCH if _matches(m.metric, tokens)), None)
            target = base.weekly
        else:
            key = next((s.key for s in SOURCES if s.match and _matches(m.metric, s.match)), None)
            target = base.secondary
            if key is not None and m.weekly_target is not None:
                base.targets.setdefault(key, float(m.weekly_target))
        if key is None:
            continue
        weeks = db.scalars(select(SheetMetricWeek).where(SheetMetricWeek.metric_id == m.id)).all()
        if not weeks:
            continue
        max_occ = max(w.occurrence for w in weeks)
        for w in weeks:
            if w.value is None or not 1 <= w.week_no <= 53:
                continue
            # The sheet carries no year: its latest repeat of a week number is the nearest past one, earlier repeats earlier years.
            year = _latest_year(today, w.week_no) - (max_occ - w.occurrence)
            try:
                date.fromisocalendar(year, w.week_no, 1)
            except ValueError:
                continue
            target.setdefault((key, year, w.week_no), float(w.value))
    return base


__all__ = [
    "SOURCES",
    "SOURCE_BY_KEY",
    "WEEKLY_ROWS",
    "SheetBaseline",
    "SourceDef",
    "manual_values",
    "set_manual",
    "set_target_override",
    "sheet_baseline",
    "target_overrides",
]
