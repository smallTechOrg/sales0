"""Plain frozen views returned by the import services (no ORM objects leave this package)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class ImportIssueView:
    """One reported problem: a row number and a stable code. Never any cell content."""

    row: int
    code: str
    level: Literal["error", "warning"] = "error"


@dataclass(frozen=True)
class ImportBatchView:
    id: int | None  # None for a dry-run preview (nothing is stored)
    filename: str
    kind: str
    rows_total: int
    rows_new: int
    rows_merged: int
    rows_unchanged: int
    rows_error: int
    errors: list[ImportIssueView] = field(default_factory=list)  # errors first, then warnings; capped
    ignored_columns: list[str] = field(default_factory=list)  # unknown header names (headers, not cells)
    dry_run: bool = False
    actor: str = "import"
    created_at: datetime | None = None


@dataclass(frozen=True)
class PasteResult:
    enquiry_id: int
    outcome: str  # "created" | "duplicate"
    verdict: str | None = None  # the AI verdict ("genuine" | "discard"); None when triage failed
    triage_status: str = "pending"  # "done" | "failed" | "pending"
    batch_id: int | None = None
