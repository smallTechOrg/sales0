"""Write-lock helper for read-modify-write sections.

SQLite (pysqlite, legacy transaction control) only opens a transaction at the first INSERT/UPDATE/DELETE, so a
``SELECT`` followed by a write can act on a row that another connection changed in between (two simultaneous
accepts both read ``pending_review``). :func:`begin_write` takes the database write lock up front
(``BEGIN IMMEDIATE``, waiting up to ``busy_timeout``), so the *re-read that follows* sees the committed truth.
On other dialects it is a no-op (they use row locks / unique constraints).
"""

from __future__ import annotations

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


__all__ = ["begin_write"]
