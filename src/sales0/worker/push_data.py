"""``sales0 push-data``: copy a local SQLite database into the hosted one through ``/api/worker/import/*``.

The source file is never written: it (and its ``-wal`` / ``-shm``) is copied to a temp directory first, and that copy
is brought to the current schema before reading. Rows go parent-first in batches; tables with a foreign-key cycle get a
second ``link`` pass (see ``sales0.services.data_import``). Re-running is safe (idempotent upsert by primary key).
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from alembic import command
from sqlalchemy import Table, func, select, text

from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, current_revision, head_revision
from sales0.logging import get_logger
from sales0.services import data_import as plan
from sales0.worker import protocol as p
from sales0.worker.client import WorkerClient

log = get_logger("sales0.worker.push_data")

BATCH_ROWS = 500
BATCH_BYTES = 3 * 1024 * 1024  # the server accepts 8 MB; stay well inside it


def copy_source(source: Path, workdir: Path) -> Path:
    """Copy the database (+ WAL/SHM) so the original is only ever read."""
    if not source.is_file():
        raise FileNotFoundError(f"{source} does not exist")
    target = workdir / "source.db"
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        extra = source.with_name(source.name + suffix)
        if extra.is_file():
            shutil.copy2(extra, workdir / ("source.db" + suffix))
    return target


def _row_size(row: dict[str, Any]) -> int:
    return len(json.dumps(row, default=str))


def batches(rows: Iterator[dict[str, Any]], max_rows: int = BATCH_ROWS, max_bytes: int = BATCH_BYTES) -> Iterator[list[dict[str, Any]]]:
    chunk: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        n = _row_size(row)
        if chunk and (len(chunk) >= max_rows or size + n > max_bytes):
            yield chunk
            chunk, size = [], 0
        chunk.append(row)
        size += n
    if chunk:
        yield chunk


def _read(conn: Any, table: Table, columns: list[str] | None = None) -> Iterator[dict[str, Any]]:
    cols = [table.c[n] for n in (columns or [c.name for c in table.columns])]
    order = [table.c[n] for n in plan.pk_names(table)]
    result = conn.execution_options(stream_results=True, yield_per=BATCH_ROWS).execute(select(*cols).order_by(*order))
    for mapping in result.mappings():
        yield dict(mapping)


def push(
    source: Path,
    client: WorkerClient | None,
    *,
    dry_run: bool = False,
    out: Callable[[str], None] = print,
) -> bool:
    """Returns True when every table's remote count covers the local one (dry-run: always True)."""
    with tempfile.TemporaryDirectory(prefix="sales0-push-") as tmp:
        copy = copy_source(source, Path(tmp))
        url = f"sqlite:///{copy}"
        engine = build_engine(url)
        try:
            if current_revision(engine) != head_revision():
                out("Source schema is behind; upgrading the COPY (your original file is untouched)...")
                command.upgrade(alembic_config(url), "head")
            return _push_engine(engine, client, dry_run, out, legacy_dirs=_legacy_dirs(source))
        finally:
            engine.dispose()


def _legacy_dirs(source: Path) -> list[Path]:
    """Where the local deck files are (``<dir>/decks/<sha>.pdf``): next to the database, then SALES0_DATA_DIR."""
    dirs = [source.resolve().parent]
    try:
        from sales0.config import get_settings

        dirs.append(get_settings().data_path)
    except Exception:  # noqa: BLE001 - no settings: the database's own directory is enough
        pass
    return list(dict.fromkeys(dirs))


def deck_bytes(conn: Any, legacy_dirs: list[Path]) -> Iterator[tuple[int, str, str, bytes | None]]:
    """(id, filename, sha256, bytes or None) of every local deck: its file ``<dir>/<stored_path>`` with the right hash."""
    for deck_id, filename, sha, stored_path in conn.execute(text("SELECT id, filename, sha256, stored_path FROM decks ORDER BY id")):
        data: bytes | None = None
        if stored_path and not Path(stored_path).is_absolute() and ".." not in Path(stored_path).parts:
            for base in legacy_dirs:
                try:
                    found = (base / stored_path).read_bytes()
                except OSError:
                    continue
                if hashlib.sha256(found).hexdigest() == sha:
                    data = found
                    break
        yield int(deck_id), str(filename), str(sha), data


def push_decks(conn: Any, client: WorkerClient, out: Callable[[str], None], legacy_dirs: list[Path]) -> int:
    """Send every deck's PDF in ``DECK_CHUNK_BYTES`` pieces (the rows went first). Returns how many are on the server."""
    done = 0
    for deck_id, filename, sha, data in deck_bytes(conn, legacy_dirs):
        if not data:
            out(f"  decks: '{filename}' has no file locally - it stays 'file missing' on the server; upload it again there.")
            continue
        step = plan.DECK_CHUNK_BYTES
        skipped = False
        for offset in range(0, len(data), step):
            res = client.import_deck_chunk(p.ImportDeckChunkRequest(
                deck_id=deck_id, sha256=sha, offset=offset, total=len(data),
                data_b64=base64.b64encode(data[offset:offset + step]).decode("ascii"),
            ))  # fmt: skip
            skipped = res.skipped
            if res.skipped or res.complete:
                break
        done += 1
        out(f"  decks: '{filename}' ({len(data) // 1024} KB) {'already there' if skipped else 'uploaded'}")
    return done


def _push_engine(
    engine: Any, client: WorkerClient | None, dry_run: bool, out: Callable[[str], None], legacy_dirs: list[Path] | None = None
) -> bool:
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        emails = {str(r[0]): r[1] for r in conn.execute(text("SELECT id, email FROM team_members"))}
        order = plan.import_order()
        for table in order:
            counts[table.name] = int(conn.execute(select(func.count()).select_from(table)).scalar_one())
        skipped = {n: int(conn.execute(text(f'SELECT count(*) FROM "{n}"')).scalar_one()) for n in sorted(plan.SKIP)}
        out("Local rows to push:")
        for name, n in counts.items():
            if n:
                out(f"  {name:28} {n:>7}")
        out("Never pushed (hosted keeps its own): " + ", ".join(f"{n}({c})" for n, c in skipped.items() if c))
        if counts.get("decks"):
            have = sum(1 for *_, data in deck_bytes(conn, legacy_dirs or []) if data is not None)
            out(f"Deck files (sent one at a time, in pieces): {have} of {counts['decks']} found locally")
        if dry_run or client is None:
            out("Dry run: nothing was sent.")
            return True
        for table in order:
            if not counts[table.name]:
                continue
            sent = 0
            for chunk in batches(_read(conn, table)):
                client.import_batch(p.ImportBatchRequest(table=table.name, phase="insert", rows=chunk, member_emails=emails))
                sent += len(chunk)
                out(f"  {table.name}: {sent}/{counts[table.name]}")
        if counts.get("decks"):
            push_decks(conn, client, out, legacy_dirs or [])
        for table in order:
            deferred = list(plan.deferred_columns(table.name))
            if not deferred or not counts[table.name]:
                continue
            cols = plan.pk_names(table) + deferred
            linked = (r for r in _read(conn, table, cols) if any(r[c] is not None for c in deferred))
            for chunk in batches(linked):
                client.import_batch(p.ImportBatchRequest(table=table.name, phase="link", rows=chunk, member_emails=emails))
            out(f"  {table.name}: links {', '.join(deferred)} set")
        report = client.import_finalize(p.ImportFinalizeRequest(counts={n: c for n, c in counts.items() if c}))
    out("Per-table check (local -> hosted):")
    for name, row in report.tables.items():
        out(f"  {'ok ' if row.ok else 'BAD'} {name:28} {row.local:>7} -> {row.remote:>7}")
    out("All tables match." if report.ok else "SOME TABLES DO NOT MATCH - see above.")
    return report.ok
