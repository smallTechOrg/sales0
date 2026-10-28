"""Diff/apply engine: parse each tab, compare row hashes, upsert changes, soft-remove gone rows."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime

from sales0.clock import utcnow
from sales0.services.sheetsync.fetch import SheetFetchError, TabSource
from sales0.services.sheetsync.parsers import parse_tab
from sales0.services.sheetsync.report import SyncReport, TabReport
from sales0.services.sheetsync.repository import RecordId, SheetRepository, SheetSource
from sales0.services.sheetsync.tabs import TabSpec

# Refuse to soft-remove more than this share of a tab's live rows in one run (a half-loaded
# or wiped sheet must not wipe the CRM). Only applies once a tab has >= MIN rows.
MAX_REMOVAL_FRACTION = 0.5
REMOVAL_GUARD_MIN_ROWS = 10


class SyncEngine:
    def __init__(
        self,
        source: TabSource,
        repository: SheetRepository,
        tabs: tuple[TabSpec, ...],
        *,
        today: Callable[[], date] | None = None,
        now: Callable[[], datetime] = utcnow,
        allow_mass_removal: bool = False,
    ) -> None:
        self._source = source
        self._repo = repository
        self._tabs = tabs
        self._now = now
        self._today = today or (lambda: now().date())
        self._allow_mass = allow_mass_removal

    def run(self, *, dry_run: bool = False, only_tab: str | None = None) -> SyncReport:
        tabs = self._tabs
        if only_tab:
            tabs = tuple(t for t in tabs if t.name.casefold() == only_tab.strip().casefold())
            if not tabs:
                raise KeyError(only_tab)
        report = SyncReport(dry_run=dry_run, started_at=self._now())
        t0 = time.monotonic()
        seen_across: set[RecordId] = set()
        collisions: set[RecordId] = set()
        for tab in tabs:
            report.tabs[tab.name] = self._sync_tab(tab, dry_run, seen_across, collisions)
        report.cross_tab_collisions = len(collisions)
        report.duration_s = time.monotonic() - t0
        report.finished_at = self._now()
        return report

    def _sync_tab(self, tab: TabSpec, dry_run: bool, seen_across: set[RecordId], collisions: set[RecordId]) -> TabReport:
        tr = TabReport(tab=tab.name, kind=tab.kind)
        try:
            text = self._source.fetch(tab)
        except SheetFetchError as exc:
            tr.fetch_error = exc.code
            return tr
        except Exception as exc:  # noqa: BLE001 — one tab must never abort the others
            tr.fetch_error = f"fetch-failed:{type(exc).__name__}"
            return tr
        parsed = parse_tab(text, tab, today=self._today())
        tr.rows, tr.blank_rows = parsed.data_rows, parsed.blank_rows
        tr.parsed = len(parsed.rows)
        tr.errors, tr.warnings = parsed.errors, parsed.warnings
        if parsed.fatal:
            tr.fetch_error = parsed.fatal  # unusable tab: report, and never remove from it
            return tr

        present: set[RecordId] = set()
        for row in parsed.rows:
            rid = row.record.record_id
            present.add(rid)
            if rid in seen_across:
                collisions.add(rid)
            seen_across.add(rid)
            src = SheetSource(tab.sheet_id, tab.gid, row.row_number, row.row_hash, tab.name)
            stored = self._repo.get_by_key(*rid)
            current = stored.sources.get(tab.tab_key) if stored else None
            if current is None:
                tr.added += 1
            elif current.row_hash == row.row_hash and stored is not None and stored.sheet_removed_at is None:
                tr.unchanged += 1
                if current.row_number != row.row_number and not dry_run:
                    self._repo.refresh_source(*rid, src)
                continue
            else:
                tr.updated += 1
            if not dry_run:
                self._repo.upsert(row.record, src)

        gone = self._repo.list_keys_for_tab(tab.sheet_id, tab.gid) - present
        if gone:
            live = len(present) + len(gone)
            if not self._allow_mass and live >= REMOVAL_GUARD_MIN_ROWS and len(gone) > MAX_REMOVAL_FRACTION * live:
                tr.removal_blocked = f"{len(gone)}-of-{live}"
            else:
                tr.removed = len(gone) if dry_run else self._repo.mark_removed(tab.sheet_id, tab.gid, gone)
        return tr
