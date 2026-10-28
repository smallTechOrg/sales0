"""The persistence seam the sync engine talks to, plus an in-memory implementation.

OWNERSHIP CONTRACT (any real implementation MUST honour it)
-----------------------------------------------------------
* SHEET-OWNED fields = exactly ``record.sheet_fields()``. ``upsert`` overwrites these and
  nothing else. The sheet wins every conflict on them.
* CRM-OWNED data is never touched by a sync: timeline, AI verdicts/fields, app notes,
  flags, stage/owner changes made in the app, tags, follow-ups. ``sheet_status`` and
  ``sheet_notes`` are sheet-owned *copies*; the CRM's own ``status`` / ``notes`` are not.
* ``upsert`` creates the entity when absent, restores it (clears ``sheet_removed_at``)
  when it had been soft-removed, and records the per-tab ``SheetSource``.
* An entity may be fed by several tabs (the same person in Outreach and Prospects). It
  keeps one ``SheetSource`` per ``(sheet_id, gid)``; sheet fields are the overlay of each
  tab's fields in ``(sheet_id, gid)`` order, later tabs winning only where non-None.
* ``mark_removed`` is SOFT: it drops that tab's source; once no live source remains the
  entity's ``sheet_removed_at`` is set. Never hard-delete.
* Every sheet-driven create / update / remove / restore must write a timeline event with
  actor ``"sheet-sync"`` (the in-memory repo records them in ``events``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sales0.clock import utcnow
from sales0.services.sheetsync.records import SheetRecord

RecordId = tuple[str, str]  # (entity, key)
TabKey = tuple[str, str]  # (sheet_id, gid)
ACTOR = "sheet-sync"


@dataclass(frozen=True)
class SheetSource:
    sheet_id: str
    gid: str
    row_number: int
    row_hash: str
    tab_name: str = ""

    @property
    def tab_key(self) -> TabKey:
        return (self.sheet_id, self.gid)


@dataclass
class StoredRecord:
    entity: str
    key: str
    fields: dict[str, Any] = field(default_factory=dict)  # merged sheet-owned view
    per_tab: dict[TabKey, dict[str, Any]] = field(default_factory=dict)
    sources: dict[TabKey, SheetSource] = field(default_factory=dict)  # live sources only
    sheet_removed_at: datetime | None = None
    crm: dict[str, Any] = field(default_factory=dict)  # CRM-owned; sync never touches it


@runtime_checkable
class SheetRepository(Protocol):
    def get_by_key(self, entity: str, key: str) -> StoredRecord | None: ...

    def upsert(self, record: SheetRecord, sheet_source: SheetSource) -> None: ...

    def refresh_source(self, entity: str, key: str, sheet_source: SheetSource) -> None:
        """Row moved but its content hash is unchanged: update row_number only, no event."""
        ...

    def mark_removed(self, sheet_id: str, gid: str, ids: Iterable[RecordId]) -> int: ...

    def list_keys_for_tab(self, sheet_id: str, gid: str) -> set[RecordId]:
        """Ids with a live (not removed) source in that tab."""
        ...


class InMemorySheetRepository:
    """Reference implementation used by tests and by the CLI when no CRM repo exists."""

    def __init__(self, clock: Callable[[], datetime] = utcnow) -> None:
        self._clock = clock
        self.records: dict[RecordId, StoredRecord] = {}
        self.events: list[dict[str, Any]] = []

    def get_by_key(self, entity: str, key: str) -> StoredRecord | None:
        return self.records.get((entity, key))

    def _merge(self, stored: StoredRecord) -> None:
        merged: dict[str, Any] = {}
        for tab_key in sorted(stored.per_tab):
            for name, value in stored.per_tab[tab_key].items():
                if value is not None:
                    merged[name] = value
                else:
                    merged.setdefault(name, None)
        stored.fields = merged

    def upsert(self, record: SheetRecord, sheet_source: SheetSource) -> None:
        rid = record.record_id
        stored = self.records.get(rid)
        action = "updated"
        if stored is None:
            stored = self.records[rid] = StoredRecord(entity=rid[0], key=rid[1])
            action = "created"
        elif stored.sheet_removed_at is not None:
            action = "restored"
        stored.per_tab[sheet_source.tab_key] = record.sheet_fields()
        stored.sources[sheet_source.tab_key] = sheet_source
        stored.sheet_removed_at = None
        self._merge(stored)
        self.events.append({"actor": ACTOR, "action": action, "entity": rid[0], "key": rid[1], "at": self._clock()})

    def refresh_source(self, entity: str, key: str, sheet_source: SheetSource) -> None:
        stored = self.records.get((entity, key))
        if stored is not None:
            stored.sources[sheet_source.tab_key] = sheet_source

    def mark_removed(self, sheet_id: str, gid: str, ids: Iterable[RecordId]) -> int:
        tab_key = (sheet_id, gid)
        count = 0
        for rid in ids:
            stored = self.records.get(rid)
            if stored is None or tab_key not in stored.sources:
                continue
            del stored.sources[tab_key]
            count += 1
            if not stored.sources and stored.sheet_removed_at is None:
                stored.sheet_removed_at = self._clock()
                self.events.append(
                    {"actor": ACTOR, "action": "removed", "entity": rid[0], "key": rid[1], "at": stored.sheet_removed_at}
                )
        return count

    def list_keys_for_tab(self, sheet_id: str, gid: str) -> set[RecordId]:
        tab_key = (sheet_id, gid)
        return {rid for rid, s in self.records.items() if tab_key in s.sources}
