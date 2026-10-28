"""Write-lock helper for read-modify-write sections.

SQLite (pysqlite, legacy transaction control) only opens a transaction at the first INSERT/UPDATE/DELETE, so a
``SELECT`` followed by a write can act on a row that another connection changed in between (two simultaneous
accepts both read ``pending_review``). :func:`begin_write` takes the database write lock up front
(``BEGIN IMMEDIATE``, waiting up to ``busy_timeout``), so the *re-read that follows* sees the committed truth.
On PostgreSQL it is a no-op: callers lock the rows they read with ``select(...).with_for_update()`` (a no-op on
SQLite), see :func:`for_update`; process-wide critical sections use :func:`advisory_lock`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session


def begin_write(db: Session) -> None:
    """Acquire the write lock now. Safe to call repeatedly and inside a transaction that already writes."""
    bind = db.get_bind()
    if bind.dialect.name != "sqlite":
        return
    conn = db.connection()
    raw = conn.connection.driver_connection  # the sqlite3 connection
    if raw is None or getattr(raw, "in_transaction", False):
        return  # a transaction is already open: it holds (or will upgrade to) the write lock on its first write
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def for_update(stmt):  # noqa: ANN001, ANN201
    """``SELECT ... FOR UPDATE`` on PostgreSQL (row locks make a read-modify-write safe); SQLite ignores it
    (``begin_write`` already holds the database write lock there)."""
    return stmt.with_for_update()


@contextmanager
def advisory_lock(db: Session, key: int, *, wait: bool = True) -> Iterator[bool]:
    """A transaction-scoped PostgreSQL advisory lock (released at commit/rollback). Yields whether it was taken.

    On SQLite (one process per data dir) it always yields True: ``begin_write`` serialises writers there.
    """
    if db.get_bind().dialect.name != "postgresql":
        yield True
        return
    if wait:
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
        yield True
    else:
        yield bool(db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": key}).scalar())


__all__ = ["advisory_lock", "begin_write", "for_update"]
