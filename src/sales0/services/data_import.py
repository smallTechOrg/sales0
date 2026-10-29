"""One-off bulk import of a local SQLite database into the hosted one (``sales0 push-data`` -> ``/api/worker/import/*``).

The hosted database is private-IP only, so the laptop pushes rows THROUGH the hosted app. Both sides use this module's
table plan, so it is derived from the ORM metadata and cannot drift:

* ``import_order()`` - parents before children. The few genuine FK cycles are cut at ``DEFERRED`` columns: the
  first pass (``insert``) writes those columns as NULL, the second (``link``) fills them in.
* rows keep their primary keys, so the import is an idempotent upsert (re-running changes nothing) and every foreign
  key between imported tables stays valid; afterwards PostgreSQL sequences are moved past ``MAX(id)``.
* ``SKIP`` lists tables that are never imported (secrets, sessions, machine-local state). References to
  ``team_members`` are re-mapped by e-mail (team members are NOT imported: the hosted app has its own accounts); a
  reference whose member does not exist on the server becomes NULL.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Date, DateTime, Table, func, select, tuple_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

import sales0.db.models  # noqa: F401  (registers every table on the metadata)
from sales0.config import get_settings
from sales0.db.base import Base
from sales0.db.pgseq import sync_sequences
from sales0.errors import DomainError
from sales0.logging import get_logger

log = get_logger("sales0.services.data_import")

MEMBERS = "team_members"
ACCOUNTS = "linkedin_accounts"

# table -> why it is never imported
SKIP: dict[str, str] = {
    "team_members": "hosted has its own accounts (references are re-mapped by e-mail)",
    "member_sessions": "login sessions",
    "password_tokens": "one-time set-password links",
    "gmail_connections": "encrypted Gmail OAuth tokens: each member reconnects on the hosted app",
    "reply_poll_state": "per-member Gmail polling cursor",
    "linkedin_session": "legacy LinkedIn session singleton",
    "linkedin_accounts": "cool-down / daily-counter state of THIS machine's login; the hosted account row stays",
    "scheduled_jobs": "hosted mode has no scheduler",
    "workers": "worker heartbeat registry",
    "app_meta": "per-database bookkeeping",
    "sheet_sources": "hosted configures its own sheets",
    "sheet_sync_state": "hosted keeps its own sheet-sync cursors",
}
# (table, column) edges cut to break FK cycles; filled in by the second ("link") pass.
DEFERRED: dict[str, tuple[str, ...]] = {
    "seen_posts": ("enquiry_id",),
    "contacts": ("merged_into_id",),
    "prospects": ("origin_enquiry_id",),
    "outreach_messages": ("follow_up_of_id",),
}
# Columns the SERVER owns: never overwritten by a re-run; a new row starts "file missing" until its file arrives.
SERVER_OWNED: dict[str, tuple[str, ...]] = {"decks": ("storage", "stored_path", "content_missing")}
# FK targets that are not imported but are checked / re-mapped on the server.
EXTERNAL = frozenset({MEMBERS, ACCOUNTS})

MAX_ROWS_PER_BATCH = 1000
# Deck FILES travel separately (``import_deck_chunk``): one deck at a time, in pieces under the 8 MB request cap. The
# hosted app writes them to its attachment store (GCS), so the laptop needs no Google credentials.
DECK_CHUNK_BYTES = 4 * 1024 * 1024  # raw bytes per chunk: ~5.4 MB as base64
DECK_MAX_TOTAL = 10 * 1024 * 1024  # the hard ceiling of SALES0_DECK_MAX_BYTES


def importable_tables() -> dict[str, Table]:
    return {t.name: t for t in Base.metadata.sorted_tables if t.name not in SKIP}


def import_order() -> list[Table]:
    """Importable tables, every parent before its children (cycles cut at ``DEFERRED``); stable by name."""
    tables = importable_tables()
    deps: dict[str, set[str]] = {}
    for name, table in tables.items():
        found: set[str] = set()
        for col in table.columns:
            if col.name in DEFERRED.get(name, ()):
                continue
            for fk in col.foreign_keys:
                target = fk.column.table.name
                if target != name and target in tables:
                    found.add(target)
        deps[name] = found
    order: list[Table] = []
    done: set[str] = set()
    while len(done) < len(tables):
        ready = sorted(n for n, d in deps.items() if n not in done and d <= done)
        if not ready:
            raise RuntimeError(f"foreign-key cycle among {sorted(set(tables) - done)}: add a DEFERRED column")
        for name in ready:
            order.append(tables[name])
            done.add(name)
    return order


def pk_names(table: Table) -> list[str]:
    return [c.name for c in table.primary_key.columns]


def deferred_columns(table_name: str) -> tuple[str, ...]:
    return DEFERRED.get(table_name, ())


def _target(column: Any) -> str | None:
    for fk in column.foreign_keys:
        return str(fk.column.table.name)
    return None


def _coerce_value(column: Any, value: Any) -> Any:
    """JSON gives datetimes as strings: parse them by the column's type; everything else passes through."""
    if value is None or not isinstance(value, str):
        return value
    impl = getattr(column.type, "impl", column.type)  # UTCDateTime is a TypeDecorator over DateTime
    kind = impl if isinstance(impl, type) else type(impl)
    try:
        if issubclass(kind, DateTime):
            return datetime.fromisoformat(value)
        if issubclass(kind, Date):
            return date.fromisoformat(value)
    except ValueError as exc:
        raise DomainError("bad_row", f"{column.table.name}.{column.name}: not a valid date/time.", 422) from exc
    return value


class _Refs:
    """Server-side resolution of references to tables that are not imported."""

    def __init__(self, db: Session, member_emails: dict[str, str]) -> None:
        self.db = db
        self.member_emails = member_emails
        self._members: dict[str, int] = {}
        self._accounts: set[int] | None = None

    def member(self, local_id: int) -> int | None:
        email = (self.member_emails.get(str(local_id)) or "").strip().lower()
        if not email:
            return None
        if email not in self._members:
            row = self.db.execute(
                select(_members_table().c.id).where(func.lower(_members_table().c.email) == email)
            ).first()
            self._members[email] = row[0] if row else 0
        return self._members[email] or None

    def account(self, local_id: int) -> int | None:
        if self._accounts is None:
            table = Base.metadata.tables[ACCOUNTS]
            self._accounts = {r[0] for r in self.db.execute(select(table.c.id))}
        return local_id if local_id in self._accounts else None


def _members_table() -> Table:
    return Base.metadata.tables[MEMBERS]


def _row_values(table: Table, row: dict[str, Any], refs: _Refs, phase: str) -> dict[str, Any]:
    cols = {c.name: c for c in table.columns}
    unknown = sorted(set(row) - set(cols))
    if unknown:
        raise DomainError("bad_row", f"{table.name}: unknown column(s) {unknown}; the laptop and the server run different versions.", 422)
    deferred = set(deferred_columns(table.name))
    pks = set(pk_names(table))
    out: dict[str, Any] = {}
    for name, value in row.items():
        col = cols[name]
        if phase == "insert" and name in deferred:
            value = None
        elif phase == "link" and name not in deferred and name not in pks:
            continue
        else:
            value = _coerce_value(col, value)
        target = _target(col)
        if value is not None and target == MEMBERS:
            value = refs.member(int(value))
        elif value is not None and target == ACCOUNTS:
            value = refs.account(int(value))
        if value is None and not col.nullable and name not in pks and not (col.default or col.server_default):
            raise DomainError("bad_row", f"{table.name}.{name} cannot be empty (a referenced team member / account is missing on the server).", 422)
        out[name] = value
    if table.name == "seen_posts" and phase == "insert":
        _apply_raw_retention(out)
    missing = pks - set(out)
    if missing:
        raise DomainError("bad_row", f"{table.name}: primary key {sorted(missing)} missing.", 422)
    return out


def _apply_raw_retention(values: dict[str, Any]) -> None:
    """Imported posts follow the HOSTED raw-text retention: a purge date earlier than first_seen + retention is extended,
    so raising ``SALES0_SEEN_RAW_RETENTION_DAYS`` before the push keeps the raw text instead of purging it next run."""
    days = get_settings().seen_raw_retention_days
    if days <= 0:  # the hosted policy is "keep forever"
        values["raw_purge_after"] = None
        return
    first, due = values.get("first_seen_at"), values.get("raw_purge_after")
    if values.get("raw_text") and isinstance(first, datetime) and isinstance(due, datetime):
        values["raw_purge_after"] = max(due, first + timedelta(days=days))


def _existing(db: Session, table: Table, rows: list[dict[str, Any]]) -> set[tuple[Any, ...]]:
    pks = [table.c[n] for n in pk_names(table)]
    keys = [tuple(r[n] for n in pk_names(table)) for r in rows]
    if len(pks) == 1:
        found = db.execute(select(pks[0]).where(pks[0].in_([k[0] for k in keys])))
    else:
        found = db.execute(select(*pks).where(tuple_(*pks).in_(keys)))
    return {tuple(r) for r in found}


def import_batch(db: Session, table_name: str, phase: str, rows: list[dict[str, Any]], member_emails: dict[str, str]) -> dict[str, int]:
    table = importable_tables().get(table_name)
    if table is None:
        raise DomainError("table_not_allowed", f"'{table_name}' is not an importable table.", 422)
    if phase not in ("insert", "link"):
        raise DomainError("bad_phase", "phase must be 'insert' or 'link'.", 422)
    if len(rows) > MAX_ROWS_PER_BATCH:
        raise DomainError("batch_too_large", f"At most {MAX_ROWS_PER_BATCH} rows per batch.", 422)
    if phase == "link" and not deferred_columns(table_name):
        return {"received": len(rows), "inserted": 0, "updated": 0}
    if not rows:
        return {"received": 0, "inserted": 0, "updated": 0}
    refs = _Refs(db, member_emails)
    values = [_row_values(table, r, refs, phase) for r in rows]
    pks = pk_names(table)
    deferred = set(deferred_columns(table_name))
    present = _existing(db, table, values)
    new = [v for v in values if tuple(v[n] for n in pks) not in present]
    old = [v for v in values if tuple(v[n] for n in pks) in present]
    owned = set(SERVER_OWNED.get(table_name, ()))
    if owned and phase == "insert":
        new = [{**v, "content_missing": True} if "content_missing" in owned else v for v in new]
        old = [{k: x for k, x in v.items() if k not in owned} for v in old]
    try:
        if new and phase == "insert":
            # one executemany needs identical keys on every row; the client sends every column, so they match
            db.execute(table.insert(), new)
        for v in old if phase == "insert" else values:
            set_ = {k: x for k, x in v.items() if k not in pks and (phase == "link" or k not in deferred)}
            if set_:
                db.execute(table.update().where(*[table.c[n] == v[n] for n in pks]).values(**set_))
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        first = str(exc.orig).splitlines()[0][:200] if exc.orig else "integrity error"
        log.warning("import_batch_rejected", table=table_name, phase=phase, rows=len(rows))
        raise DomainError("import_conflict", f"{table_name}: {first}", 422) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        first = str(getattr(exc, "orig", exc)).splitlines()[0][:200]
        log.warning("import_batch_failed", table=table_name, phase=phase, rows=len(rows))
        raise DomainError("import_failed", f"{table_name}: {first}", 422) from exc
    log.info("import_batch", table=table_name, phase=phase, rows=len(rows), inserted=len(new) if phase == "insert" else 0)
    return {"received": len(rows), "inserted": len(new) if phase == "insert" else 0, "updated": len(old) if phase == "insert" else len(values)}


def _refresh_company_lifecycle(db: Session) -> None:
    """Rows arrive through Core upserts (no ORM flush), and a laptop on an older schema sends no company stage columns:
    re-derive every company's stage / status from its prospect / lead / client rows once the import is complete."""
    from sales0.db import company_projection
    from sales0.db.models import Client, Lead, Prospect

    ids: set[int] = set()
    for model in (Prospect, Lead, Client):
        ids |= {int(i) for i in db.execute(select(model.company_id).where(model.company_id.is_not(None)).distinct()).scalars()}
    if ids:
        company_projection.recompute(db.connection(), ids, actor="import")


def finalize(db: Session, local_counts: dict[str, int]) -> dict[str, Any]:
    """Compare row counts and (PostgreSQL) move every id sequence past MAX(id)."""
    tables = importable_tables()
    unknown = sorted(set(local_counts) - set(tables))
    if unknown:
        raise DomainError("table_not_allowed", f"Not importable: {unknown}.", 422)
    sync_sequences(db.connection())
    _refresh_company_lifecycle(db)
    report: dict[str, dict[str, Any]] = {}
    for name, local in local_counts.items():
        remote = db.execute(select(func.count()).select_from(tables[name])).scalar_one()
        report[name] = {"local": int(local), "remote": int(remote), "ok": remote >= local}
    return {"tables": report, "ok": all(r["ok"] for r in report.values())}


def _part_key(sha256: str, offset: int) -> str:
    return f"decks/_parts/{sha256}/{offset:012d}"


def import_deck_chunk(db: Session, *, deck_id: int, sha256: str, offset: int, total: int, data: bytes) -> dict[str, Any]:
    """Store one piece of a deck's PDF (its row is imported first). Pieces arrive in order and are kept as temporary
    objects; the last one assembles ``decks/<sha256>.pdf`` (sha256 verified) in the hosted store, marks the row
    available and removes the pieces. ``offset == 0`` with the file already stored answers ``skipped``: re-running
    push-data sends nothing twice."""
    from sales0.db.models import Deck
    from sales0.services.outreach import storage

    deck = db.get(Deck, deck_id)
    if deck is None:
        raise DomainError("deck_unknown", f"Deck {deck_id} is not on the server; push the decks rows first.", 422)
    if sha256 != deck.sha256:
        raise DomainError("deck_hash_mismatch", f"Deck {deck_id}: the file does not match the server's row.", 422)
    if not 0 < total <= DECK_MAX_TOTAL or offset < 0 or not data or offset + len(data) > total:
        raise DomainError("deck_chunk_invalid", f"Deck {deck_id}: chunk outside the declared size.", 422)
    backend = storage.current_backend()  # hosted without a bucket: refused loudly
    store = storage.store_for(backend)
    key = storage.deck_key(sha256)
    if offset == 0 and store.exists(key):
        deck.storage, deck.stored_path, deck.content_missing = backend, key, False
        db.flush()
        return {"stored": total, "complete": True, "skipped": True}
    if offset > 0 and not store.exists(_part_key(sha256, 0)):
        raise DomainError("deck_chunk_order", f"Deck {deck_id}: chunk at {offset} out of order; re-run push-data.", 409)
    store.put(_part_key(sha256, offset), data)
    if offset + len(data) < total:
        return {"stored": offset + len(data), "complete": False, "skipped": False}
    pieces: list[bytes] = []
    offsets: list[int] = []
    at = 0
    while at < total:
        piece = store.get(_part_key(sha256, at))
        if not piece:
            raise DomainError("deck_chunk_order", f"Deck {deck_id}: piece at {at} is missing; re-run push-data.", 409)
        pieces.append(piece)
        offsets.append(at)
        at += len(piece)
    whole = b"".join(pieces)
    for off in offsets:
        store.delete(_part_key(sha256, off))
    if len(whole) != total or hashlib.sha256(whole).hexdigest() != sha256:
        raise DomainError("deck_hash_mismatch", f"Deck {deck_id}: the uploaded bytes do not match its sha256.", 422)
    store.put(key, whole)
    deck.storage, deck.stored_path, deck.content_missing = backend, key, False
    db.flush()
    log.info("import_deck_file", deck_id=deck_id, size=total)
    return {"stored": total, "complete": True, "skipped": False}
