"""Read-only export of the hosted database (``/api/worker/export/*`` <- ``sales0 pull-data``).

The reverse of ``data_import``: the hosted database is private-IP only, so a developer's laptop pulls rows THROUGH the
hosted app to build a local SQLite copy. It shares the importer's table plan, so the two cannot drift:

* the exported tables are exactly ``data_import.importable_tables()`` - the ``SKIP`` set (team members and passwords,
  login sessions, one-time tokens, Gmail tokens, LinkedIn account/session state, workers, app_meta, sheet sources and
  sync state, ...) is NEVER exported;
* rows are paged by primary key (keyset, never OFFSET), each page capped at ``MAX_BATCH_BYTES`` of JSON;
* values are JSON-safe: datetimes/dates as ISO strings, Decimal as a string, bytes as base64, JSON columns as JSON;
  the importer's ``_coerce_value`` parses the date strings back by column type;
* nothing is written: PostgreSQL transactions are switched to ``READ ONLY``.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Table, func, select, text, tuple_
from sqlalchemy.orm import Session

from sales0.db.migrate_check import head_revision
from sales0.errors import DomainError
from sales0.services import data_import as plan

MAX_BATCH_BYTES = 4 * 1024 * 1024  # JSON bytes of one response page
MAX_BATCH_ROWS = 1000
DEFAULT_BATCH_ROWS = 500
# Only these may be dropped, and only from a single row that alone exceeds MAX_BATCH_BYTES.
TRIMMABLE_COLUMNS = ("raw_card", "raw_text")


def encode_value(value: Any) -> Any:
    """JSON-safe form of one cell (recursive for JSON columns)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {str(k): encode_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [encode_value(v) for v in value]
    return str(value)


def encode_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: encode_value(v) for k, v in row.items()}


def row_bytes(row: dict[str, Any]) -> int:
    return len(json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def encode_cursor(values: list[Any]) -> str:
    return json.dumps(values, separators=(",", ":"))


def decode_cursor(raw: str | None, table: Table) -> list[Any] | None:
    """``None``/empty = from the start. Otherwise a JSON list with one value per primary-key column."""
    if raw is None or raw == "":
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise DomainError("bad_cursor", "after must be a JSON list of primary-key values.", 422) from exc
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        value = [value]
    n = len(plan.pk_names(table))
    if not isinstance(value, list) or len(value) != n or not all(isinstance(v, (int, str)) and not isinstance(v, bool) for v in value):
        raise DomainError("bad_cursor", f"after needs {n} primary-key value(s) for {table.name}.", 422)
    return value


def exportable_table(name: str) -> Table:
    table = plan.importable_tables().get(name)
    if table is None:
        raise DomainError("table_not_allowed", f"'{name}' is not an exportable table.", 422)
    return table


def begin_read_only(db: Session) -> None:
    """Must be the first statement of the transaction. PostgreSQL then refuses any write."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SET TRANSACTION READ ONLY"))


def manifest(db: Session) -> dict[str, Any]:
    begin_read_only(db)
    tables = []
    for table in plan.import_order():
        n = int(db.execute(select(func.count()).select_from(table)).scalar_one())
        tables.append({"name": table.name, "rows": n, "pk": plan.pk_names(table)})
    return {
        "head": db.execute(text("SELECT version_num FROM alembic_version")).scalar(),
        "code_head": head_revision(),
        "tables": tables,
        "excluded": sorted(plan.SKIP),
        "max_batch_rows": MAX_BATCH_ROWS,
        "max_batch_bytes": MAX_BATCH_BYTES,
    }


def batch(
    db: Session, table_name: str, after: str | None = None, limit: int = DEFAULT_BATCH_ROWS, *, max_bytes: int = MAX_BATCH_BYTES
) -> dict[str, Any]:
    """One page of ``table_name`` after the cursor. ``next`` is the cursor of the following page, or None at the end."""
    table = exportable_table(table_name)
    if not 1 <= limit <= MAX_BATCH_ROWS:
        raise DomainError("bad_limit", f"limit must be between 1 and {MAX_BATCH_ROWS}.", 422)
    cursor = decode_cursor(after, table)
    begin_read_only(db)
    pks = [table.c[n] for n in plan.pk_names(table)]
    query = select(table).order_by(*pks).limit(limit + 1)
    if cursor is not None:
        query = query.where(pks[0] > cursor[0] if len(pks) == 1 else tuple_(*pks) > tuple_(*cursor))
    fetched = [dict(r) for r in db.execute(query).mappings()]
    more = len(fetched) > limit
    fetched = fetched[:limit]
    rows: list[dict[str, Any]] = []
    used = 0
    trimmed = 0
    cut = False
    for raw in fetched:
        row = encode_row(raw)
        size = row_bytes(row)
        if size > max_bytes:  # a single huge row: drop the raw capture, keep everything else
            for col in TRIMMABLE_COLUMNS:
                if col in row and row[col] is not None:
                    row[col] = None
            size = row_bytes(row)
            trimmed += 1
        if rows and used + size > max_bytes:
            cut = True
            break
        rows.append(row)
        used += size
    nxt = None
    if rows and (cut or more):
        last = rows[-1]
        nxt = encode_cursor([last[n] for n in plan.pk_names(table)])
    return {
        "table": table.name,
        "columns": [c.name for c in table.columns],
        "rows": rows,
        "next": nxt,
        "trimmed": trimmed,
    }
