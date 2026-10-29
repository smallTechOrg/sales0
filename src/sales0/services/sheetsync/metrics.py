"""Metrics tabs (Weekly / Secondary) -> ``sheet_metrics`` + ``sheet_metric_weeks`` (the Phase 4 baseline).

A row is replaced wholesale when its hash changed (the weeks are deleted and re-inserted). No
timeline events: a metric has no timeline subject. The sheet gives no year; ``week_no`` is stored as is.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from sales0.db.models.sheetdata import SheetMetric, SheetMetricWeek
from sales0.services._common import clip
from sales0.services.sheetsync.mapping import ApplyResult, json_fields
from sales0.services.sheetsync.records import SheetMetricRow


def apply_metric(db: Session, record: SheetMetricRow, *, now: datetime) -> ApplyResult:
    key = record.key[:300]
    row = db.scalar(select(SheetMetric).where(SheetMetric.metric_key == key))
    created, restored = row is None, False
    if row is None:
        row = SheetMetric(metric_set=record.metric_set, metric=clip(record.metric, 200) or "(unnamed)", metric_key=key)
        db.add(row)
        db.flush()
    elif row.sheet_removed_at is not None:
        restored = True
    changed: list[str] = []
    for column, value in (
        ("metric_set", record.metric_set),
        ("group_label", clip(record.group, 200)),
        ("metric", clip(record.metric, 200) or "(unnamed)"),
        ("weekly_target", record.weekly_target),
        ("weekly_target_raw", clip(record.weekly_target_raw, 60)),
        ("sheet_removed_at", None),
    ):
        if getattr(row, column) != value:
            setattr(row, column, value)
            changed.append(column)
    row.sheet_fields = json_fields(record)
    wanted = {
        (w.week_no, w.occurrence): (clip(w.label, 20) or "", clip(w.raw, 60) or "", w.value) for w in record.weeks
    }
    have = {
        (w.week_no, w.occurrence): (w.label, w.raw, w.value)
        for w in db.scalars(select(SheetMetricWeek).where(SheetMetricWeek.metric_id == row.id))
    }
    if wanted != have:
        db.execute(delete(SheetMetricWeek).where(SheetMetricWeek.metric_id == row.id))
        db.flush()
        for (week_no, occurrence), (label, raw, value) in wanted.items():
            db.add(SheetMetricWeek(metric_id=row.id, week_no=week_no, occurrence=occurrence, label=label, raw=raw, value=value))
        changed.append("weeks")
    db.flush()
    if created:
        return ApplyResult("created", tuple(changed), "sheet_metric", row.id)
    if restored:
        return ApplyResult("restored", tuple(changed), "sheet_metric", row.id)
    return ApplyResult("updated" if changed else "unchanged", tuple(changed), "sheet_metric", row.id)
