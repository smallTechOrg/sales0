"""Sync result types. Counts and row numbers only — never cell content."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sales0.services.sheetsync.parsers import RowIssue

MAX_ISSUES_IN_REPORT = 50


@dataclass
class TabReport:
    tab: str
    kind: str
    rows: int = 0  # non-blank data rows read
    blank_rows: int = 0
    parsed: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    errors: list[RowIssue] = field(default_factory=list)
    warnings: list[RowIssue] = field(default_factory=list)
    fetch_error: str | None = None  # stable code; set => nothing was removed
    removal_blocked: str | None = None
    unknown_statuses: int = 0  # rows whose non-blank Status matched nothing (mapped to New; not an error)

    @property
    def changes(self) -> int:
        return self.added + self.updated + self.removed

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["errors"] = [asdict(i) for i in self.errors[:MAX_ISSUES_IN_REPORT]]
        d["warnings"] = [asdict(i) for i in self.warnings[:MAX_ISSUES_IN_REPORT]]
        d["error_count"] = len(self.errors)
        d["warning_count"] = len(self.warnings)
        return d

    def summary(self) -> str:
        s = (
            f"{self.tab}: rows={self.rows} added={self.added} updated={self.updated} "
            f"unchanged={self.unchanged} removed={self.removed} errors={len(self.errors)} "
            f"warnings={len(self.warnings)}"
        )
        if self.fetch_error:
            s += f" FETCH-ERROR={self.fetch_error}"
        if self.unknown_statuses:
            s += f" unknown-statuses={self.unknown_statuses}"
        if self.removal_blocked:
            s += f" REMOVAL-BLOCKED={self.removal_blocked}"
        return s


@dataclass
class SyncReport:
    dry_run: bool
    started_at: datetime
    finished_at: datetime | None = None
    duration_s: float = 0.0
    tabs: dict[str, TabReport] = field(default_factory=dict)
    cross_tab_collisions: int = 0  # same (entity, key) seen in more than one tab this run
    error: str | None = None

    @property
    def total_changes(self) -> int:
        return sum(t.changes for t in self.tabs.values())

    @property
    def ok(self) -> bool:
        return self.error is None and not any(t.fetch_error for t in self.tabs.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_s": round(self.duration_s, 3),
            "cross_tab_collisions": self.cross_tab_collisions,
            "error": self.error,
            "ok": self.ok,
            "tabs": {name: t.to_dict() for name, t in self.tabs.items()},
        }

    def summary_lines(self) -> list[str]:
        lines = [t.summary() for t in self.tabs.values()]
        lines.append(
            f"total: changes={self.total_changes} cross_tab_collisions={self.cross_tab_collisions} "
            f"duration={self.duration_s:.1f}s dry_run={self.dry_run}"
        )
        return lines
