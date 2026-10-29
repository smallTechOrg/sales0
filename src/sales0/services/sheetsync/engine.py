"""Diff/apply engine: parse each tab, compare row hashes, upsert changes, soft-remove gone rows."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime

from sales0.clock import utcnow
from sales0.config import get_settings
from sales0.services.sheetsync.fetch import SheetFetchError, TabSource
from sales0.services.sheetsync.parsers import ParsedRow, RowIssue, parse_tab
from sales0.services.sheetsync.report import SyncReport, TabReport
from sales0.services.sheetsync.records import SheetDiscarded, SheetEnquiry, SheetOutreachPerson, SheetProspect
from sales0.services.sheetsync.status_map import map_enquiry_status, map_person_status
from sales0.services.sheetsync.repository import RecordId, SheetRepository, SheetSource
from sales0.services.sheetsync.tabs import TabSpec

# Refuse to soft-remove more than this share of a tab's live rows in one run (a half-loaded
# or wiped sheet must not wipe the CRM). Applies once a tab has >= MIN live rows; a tab that
# yields no usable data rows at all is blocked whatever its size.
MAX_REMOVAL_FRACTION = 0.5
REMOVAL_GUARD_MIN_ROWS = 3


def _count_unknown(rows: list) -> int:  # type: ignore[type-arg]
    n = 0
    for r in rows:
        rec = r.record
        status = getattr(rec, "sheet_status", None)
        if not (status or "").strip():
            continue
        if isinstance(rec, SheetEnquiry):
            if isinstance(rec, SheetDiscarded):
                continue  # discarded rows are never mapped to a prospect
            out = map_enquiry_status(status, date_email_sent=rec.date_email_sent, date_proposal_sent=rec.date_proposal_sent)
        elif isinstance(rec, SheetOutreachPerson | SheetProspect):
            out = map_person_status(status)
        else:
            continue
        n += out.unknown
    return n


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


_KEY_SOURCE_BY_PREFIX = {"post:": "post", "url:": "post", "eh:": "email_summary", "li:": "linkedin", "nc:": "name_company", "row:": "row_hash"}


def _rekey(row: ParsedRow, key: str) -> ParsedRow:
    """The row with another record key (a duplicate group's identity assignment); no-op if unchanged."""
    rec = row.record
    if rec.key == key:
        return row
    changes: dict[str, str] = {"key": key}
    if hasattr(rec, "key_source"):
        changes["key_source"] = next((src for pre, src in _KEY_SOURCE_BY_PREFIX.items() if key.startswith(pre)), "row_hash")
    return replace(row, record=replace(rec, **changes))  # type: ignore[type-var]


def _freeze_years(rec, stored_fields: dict | None):  # type: ignore[no-untyped-def]
    """Reuse the stored year of a year-less date while its day and month are unchanged (audit #4)."""
    if not isinstance(rec, SheetEnquiry) or not rec.year_inferred or not stored_fields:
        return rec
    changes = {}
    for name in rec.year_inferred:
        new, old = getattr(rec, name, None), _as_date(stored_fields.get(name))
        if new is not None and old is not None and new != old and (new.month, new.day) == (old.month, old.day):
            changes[name] = old
    return replace(rec, **changes) if changes else rec


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
        # 'today' is the configured local date (a 23:30-UTC run is already tomorrow in Asia/Kolkata)
        self._today = today or (lambda: now().astimezone(get_settings().tz).date())
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
        finish = getattr(self._repo, "finish_run", None)
        if callable(finish) and not dry_run:
            try:
                finish()  # derived data (flags) after a real run; must never fail the sync
            except Exception:  # noqa: BLE001
                pass
        report.duration_s = time.monotonic() - t0
        report.finished_at = self._now()
        return report

    @staticmethod
    def _drain(repo: SheetRepository) -> list[RowIssue]:
        drain = getattr(repo, "drain_errors", None)
        return list(drain()) if callable(drain) else []

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
        tr.unknown_statuses = _count_unknown(parsed.rows)
        tr.errors, tr.warnings = parsed.errors, parsed.warnings
        if parsed.fatal:
            tr.fetch_error = parsed.fatal  # unusable tab: report, and never remove from it
            return tr

        stored_ids = self._repo.list_keys_for_tab(tab.sheet_id, tab.gid)
        rows = self._stabilise_keys(tab, parsed.rows, stored_ids)
        present: set[RecordId] = {row.record.record_id for row in rows}
        begin = getattr(self._repo, "begin_tab", None)
        if callable(begin) and not dry_run:
            begin(tab.sheet_id, tab.gid, present)  # lets the repository tell an edited row from a brand-new one
        try:
            self._apply_rows(tab, tr, rows, dry_run, seen_across, collisions)
        finally:
            end = getattr(self._repo, "end_tab", None)
            if callable(end):
                end(tab.sheet_id, tab.gid)

        stored = self._repo.list_keys_for_tab(tab.sheet_id, tab.gid)
        gone = stored - present
        if gone:
            live = len(stored)
            empty = not present  # header but no usable data rows while live rows exist
            if not self._allow_mass and (empty or (live >= REMOVAL_GUARD_MIN_ROWS and len(gone) > MAX_REMOVAL_FRACTION * live)):
                tr.removal_blocked = f"{len(gone)}-of-{live}"
                tr.errors.append(RowIssue(0, "removal-blocked", f"{len(gone)} of {live} rows would be removed"))
            elif dry_run:
                tr.removed = len(gone)
            else:
                tr.removed = self._repo.mark_removed(tab.sheet_id, tab.gid, gone)
                tr.errors.extend(self._drain(self._repo))
        return tr

    def _stabilise_keys(self, tab: TabSpec, rows: list[ParsedRow], stored_ids: set[RecordId]) -> list[ParsedRow]:
        """Give rows that share a natural key a STABLE identity (audit #10).

        The parser keys the first such row by the natural key and re-keys the others by their row hash,
        by sheet order. Here the identity follows what is stored: a row that already lives under its own
        ``row:<hash>`` key keeps it; the natural key goes to the row whose hash the stored source holds
        (else the first). Reordering or deleting a duplicate therefore never moves content between enquiries.
        """
        out = list(rows)

        def own(r: ParsedRow) -> RecordId:
            return (r.record.ENTITY, "row:" + r.row_hash)

        for i, r in enumerate(out):
            # A lone row that used to be a re-keyed duplicate keeps its own identity - unless it is the same
            # enquiry as the natural key's (a shared post link): then it is promoted when the first row is gone.
            if r.natural_key is None and not r.record.key.startswith("row:") and own(r) in stored_ids:
                mine, natural = self._repo.get_by_key(*own(r)), self._repo.get_by_key(r.record.ENTITY, r.record.key)
                shared = mine is not None and natural is not None and mine.target_id is not None and mine.target_id == natural.target_id
                if not shared:
                    out[i] = _rekey(r, own(r)[1])
        groups: dict[RecordId, list[int]] = defaultdict(list)
        for i, r in enumerate(out):
            if r.natural_key is not None:
                groups[(r.record.ENTITY, r.natural_key)].append(i)
        for (entity, natural), idxs in groups.items():
            free = [i for i in idxs if own(out[i]) not in stored_ids]
            winner = None
            if free:
                stored = self._repo.get_by_key(entity, natural)
                src = stored.sources.get(tab.tab_key) if stored else None
                winner = next((i for i in free if src is not None and out[i].row_hash == src.row_hash), free[0])
            for i in idxs:
                out[i] = _rekey(out[i], natural if i == winner else own(out[i])[1])
        return out

    def _apply_rows(self, tab: TabSpec, tr: TabReport, rows: list[ParsedRow], dry_run: bool, seen_across: set[RecordId], collisions: set[RecordId]) -> None:
        for row in rows:
            rid = row.record.record_id
            if rid in seen_across:
                collisions.add(rid)
            seen_across.add(rid)
            src = SheetSource(tab.sheet_id, tab.gid, row.row_number, row.row_hash, tab.name)
            stored = self._repo.get_by_key(*rid)
            current = stored.sources.get(tab.tab_key) if stored else None
            record = _freeze_years(row.record, stored.per_tab.get(tab.tab_key) if stored else None)
            kind = "added"
            if current is None:
                tr.added += 1
            elif current.row_hash == row.row_hash and stored is not None and stored.sheet_removed_at is None:
                tr.unchanged += 1
                if current.row_number != row.row_number and not dry_run:
                    self._repo.refresh_source(*rid, src)
                continue
            else:
                tr.updated += 1
                kind = "updated"
            if not dry_run:
                self._repo.upsert(record, src)
                failed = self._drain(self._repo)
                if failed:
                    # The row was NOT applied (and keeps its old hash, so the next sync retries it):
                    # it moves from added/updated to the error list - no row is lost silently.
                    setattr(tr, kind, getattr(tr, kind) - 1)
                    tr.errors.extend(failed)
