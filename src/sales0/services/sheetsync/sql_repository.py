"""The CRM-backed ``SheetRepository`` (Phase 2, S2).

State lives in ``sheet_sources`` (one row per ``(entity, key, sheet_id, gid)``) plus the sheet
columns on the target tables (see ``mapping``). One short transaction per call; a record that
fails to apply rolls back alone, is reported as ``apply-failed:<ExceptionName>`` on its row and
keeps its OLD hash, so the next sync retries it. Nothing here can write to Google: this module
only touches the local database.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc, utcnow
from sales0.db.models import Enquiry
from sales0.db.models.crm import Prospect
from sales0.db.models.registries import ChannelGroup, Competitor
from sales0.db.models.sheetdata import SheetMetric
from sales0.db.models.sheetdata import SheetSource as SheetSourceRow
from sales0.db.models.timeline import TimelineEvent
from sales0.logging import get_logger
from sales0.services.sheetsync import mapping
from sales0.services.sheetsync.parsers import RowIssue
from sales0.services.sheetsync.records import SheetRecord
from sales0.services.sheetsync.registry import register_repository_factory
from sales0.services.sheetsync.repository import ACTOR, RecordId, SheetSource, StoredRecord

log = get_logger("sales0.sheetsync")

TARGET_MODELS: dict[str, type] = {
    "enquiry": Enquiry,
    "prospect": Prospect,
    "competitor": Competitor,
    "channel_group": ChannelGroup,
    "sheet_metric": SheetMetric,
}
TARGET_SUBJECT = {"enquiry": "enquiry", "prospect": "prospect", "competitor": "competitor", "channel_group": "group"}


def _actor():  # type: ignore[no-untyped-def]
    from sales0.services.crm.actor import SHEET_SYNC

    return SHEET_SYNC


class SqlSheetRepository:
    def __init__(self, session_factory: Callable[[], Session] | None = None, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._sf = session_factory
        self._clock = clock
        self._errors: list[RowIssue] = []
        self._lock = threading.Lock()
        self._touched: set[str] = set()

    # ------------------------------------------------------------------ plumbing
    def _session(self) -> Session:
        if self._sf is not None:
            return self._sf()
        from sales0.db.engine import get_sessionmaker

        return get_sessionmaker()()

    # ------------------------------------------------------------------ reads
    def get_by_key(self, entity: str, key: str) -> StoredRecord | None:
        with self._session() as db:
            rows = list(db.scalars(select(SheetSourceRow).where(SheetSourceRow.entity == entity, SheetSourceRow.key == key)))
            if not rows:
                return None
            stored = StoredRecord(entity=entity, key=key)
            for r in rows:
                tk = (r.sheet_id, r.gid)
                stored.per_tab[tk] = dict(r.fields or {})
                stored.sources[tk] = SheetSource(r.sheet_id, r.gid, r.row_number, r.row_hash, r.tab_name)
            stored.fields = mapping.overlay([stored.per_tab[k] for k in sorted(stored.per_tab)])
            model = TARGET_MODELS.get(rows[0].target_type)
            target = db.get(model, rows[0].target_id) if model else None
            stored.sheet_removed_at = getattr(target, "sheet_removed_at", None)
            return stored

    def list_keys_for_tab(self, sheet_id: str, gid: str) -> set[RecordId]:
        with self._session() as db:
            return {(e, k) for e, k in db.execute(select(SheetSourceRow.entity, SheetSourceRow.key).where(SheetSourceRow.sheet_id == sheet_id, SheetSourceRow.gid == gid))}

    # ------------------------------------------------------------------ writes
    def upsert(self, record: SheetRecord, sheet_source: SheetSource) -> None:
        now = ensure_utc(self._clock())
        with self._session() as db:
            try:
                result = mapping.apply_record(
                    db, record, origin="sheet", source=sheet_source, actor=_actor(), now=now, previous_fields=None
                )
                row = db.scalar(
                    select(SheetSourceRow).where(
                        SheetSourceRow.entity == record.ENTITY, SheetSourceRow.key == record.key,
                        SheetSourceRow.sheet_id == sheet_source.sheet_id, SheetSourceRow.gid == sheet_source.gid,
                    )
                )
                if row is None:
                    row = SheetSourceRow(
                        entity=record.ENTITY, key=record.key, sheet_id=sheet_source.sheet_id, gid=sheet_source.gid, first_seen_at=now,
                        target_type=result.target_type, target_id=result.target_id, tab_name="", row_number=0, row_hash="",
                    )
                    db.add(row)
                row.target_type, row.target_id = result.target_type, result.target_id
                row.tab_name = (sheet_source.tab_name or "")[:60]
                row.row_number, row.row_hash = sheet_source.row_number, sheet_source.row_hash
                row.fields = mapping.json_fields(record)
                row.last_seen_at = now
                db.commit()
                self._touched.add(result.target_type)
            except Exception as exc:  # noqa: BLE001 - one bad row must never stop the tab
                db.rollback()
                log.warning("sheet_apply_failed", error_type=type(exc).__name__, row=sheet_source.row_number, entity=record.ENTITY)
                with self._lock:
                    self._errors.append(RowIssue(sheet_source.row_number, f"apply-failed:{type(exc).__name__}"))

    def refresh_source(self, entity: str, key: str, sheet_source: SheetSource) -> None:
        with self._session() as db:
            row = db.scalar(
                select(SheetSourceRow).where(
                    SheetSourceRow.entity == entity, SheetSourceRow.key == key,
                    SheetSourceRow.sheet_id == sheet_source.sheet_id, SheetSourceRow.gid == sheet_source.gid,
                )
            )
            if row is not None:
                row.row_number = sheet_source.row_number
                row.last_seen_at = ensure_utc(self._clock())
                db.commit()

    def mark_removed(self, sheet_id: str, gid: str, ids: Iterable[RecordId]) -> int:
        now = ensure_utc(self._clock())
        count = 0
        for entity, key in ids:
            with self._session() as db:
                try:
                    row = db.scalar(
                        select(SheetSourceRow).where(
                            SheetSourceRow.entity == entity, SheetSourceRow.key == key,
                            SheetSourceRow.sheet_id == sheet_id, SheetSourceRow.gid == gid,
                        )
                    )
                    if row is None:
                        continue
                    row_number = row.row_number
                    target_type, target_id = row.target_type, row.target_id
                    db.execute(delete(SheetSourceRow).where(SheetSourceRow.id == row.id))
                    db.flush()
                    left = db.scalar(
                        select(func.count(SheetSourceRow.id)).where(SheetSourceRow.target_type == target_type, SheetSourceRow.target_id == target_id)
                    )
                    if not left:
                        self._soft_remove(db, target_type, target_id, now)
                    db.commit()
                    count += 1
                    self._touched.add(target_type)
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    log.warning("sheet_remove_failed", error_type=type(exc).__name__, entity=entity)
                    with self._lock:
                        self._errors.append(RowIssue(locals().get("row_number", 0), f"remove-failed:{type(exc).__name__}"))
        return count

    @staticmethod
    def _soft_remove(db: Session, target_type: str, target_id: int, now: datetime) -> None:
        model = TARGET_MODELS.get(target_type)
        target = db.get(model, target_id) if model else None
        if target is None or getattr(target, "sheet_removed_at", None) is not None:
            return
        target.sheet_removed_at = now
        subject = TARGET_SUBJECT.get(target_type)
        if subject is None:
            return  # metrics have no timeline subject
        scopes = {
            "prospect_id": target.id if target_type == "prospect" else getattr(target, "prospect_id", None),
            "enquiry_id": target.id if target_type == "enquiry" else None,
            "company_id": getattr(target, "company_id", None),
        }
        db.add(
            TimelineEvent(
                subject_type=subject, subject_id=target.id, kind="sheet_removed", body="No longer in the sheet; hidden, not deleted",
                actor=ACTOR, occurred_at=now, **scopes,
            )
        )

    # ------------------------------------------------------------------ run bookkeeping
    def drain_errors(self) -> list[RowIssue]:
        with self._lock:
            out, self._errors = self._errors, []
        return out

    def finish_run(self) -> None:
        """After a real (non-dry) run that touched enquiries or competitors: refresh the derived flags."""
        touched, self._touched = self._touched, set()
        if not touched & {"enquiry", "competitor"}:
            return
        try:
            from sales0.services.crm import flags

            with self._session() as db:
                flags.recompute_all(db, now=ensure_utc(self._clock()))
                db.commit()
        except Exception as exc:  # noqa: BLE001 - derived data must never fail a sync
            log.warning("sheet_flags_recompute_failed", error_type=type(exc).__name__)


def install(session_factory: Callable[[], Session] | None = None) -> None:
    """Register the CRM repository factory (idempotent; the app lifespan and the CLI call it)."""
    register_repository_factory(lambda: SqlSheetRepository(session_factory))


__all__ = ["SqlSheetRepository", "install"]
