"""Run bookkeeping: in-memory and SQLAlchemy (``sheet_sync_state``) stores."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from sales0.clock import utcnow
from sales0.db.models.sheet_sync import SHEET_SYNC_STATE_ID, SheetSyncState
from sales0.services.sheetsync.report import SyncReport


def _status(report: SyncReport | None, error: str | None) -> str:
    if report is None or error:
        return "failed"
    return "ok" if report.ok else "partial"


class InMemoryStateStore:
    def __init__(self) -> None:
        self.last_finished: datetime | None = None
        self.runs: list[dict[str, Any]] = []

    def load_last_finished(self) -> datetime | None:
        return self.last_finished

    def save_run(self, started_at: datetime, report: SyncReport | None, error: str | None) -> None:
        self.last_finished = report.finished_at if report and report.finished_at else utcnow()
        self.runs.append({"started_at": started_at, "status": _status(report, error), "error": error})


class SqlStateStore:
    """Persists the last run so status and the interval survive a restart."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._sf = session_factory

    def load_last_finished(self) -> datetime | None:
        with self._sf() as s:
            row = s.get(SheetSyncState, SHEET_SYNC_STATE_ID)
            return row.last_finished_at if row else None

    def load(self) -> dict[str, Any] | None:
        with self._sf() as s:
            row = s.get(SheetSyncState, SHEET_SYNC_STATE_ID)
            if row is None:
                return None
            return {
                "last_started_at": row.last_started_at,
                "last_finished_at": row.last_finished_at,
                "last_status": row.last_status,
                "last_error": row.last_error,
                "last_dry_run": row.last_dry_run,
                "last_duration_s": row.last_duration_s,
                "runs_total": row.runs_total,
                "report": json.loads(row.last_report_json) if row.last_report_json else None,
            }

    def save_run(self, started_at: datetime, report: SyncReport | None, error: str | None) -> None:
        with self._sf() as s:
            row = s.get(SheetSyncState, SHEET_SYNC_STATE_ID)
            if row is None:
                row = SheetSyncState(id=SHEET_SYNC_STATE_ID, runs_total=0)
                s.add(row)
            row.last_started_at = started_at
            row.last_finished_at = report.finished_at if report and report.finished_at else utcnow()
            row.last_status = _status(report, error)
            row.last_error = (error or "")[:500] or None
            row.last_dry_run = bool(report.dry_run) if report else False
            row.last_duration_s = report.duration_s if report else None
            row.last_report_json = json.dumps(report.to_dict()) if report else None
            row.runs_total = (row.runs_total or 0) + 1
            s.commit()
