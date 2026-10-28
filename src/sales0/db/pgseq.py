"""PostgreSQL: move every ``id`` SERIAL sequence past its table's MAX(id).

Migrations seed rows with explicit ids (offering groups, offerings, the default LinkedIn account, competitors); on
PostgreSQL that leaves the sequence at 1 and the first normal INSERT collides. ``bootstrap`` runs this after every
``alembic upgrade`` (so a future seeding migration cannot regress it); migration p10 runs the same statement.
"""

from __future__ import annotations

from sqlalchemy import Connection, text

SYNC_SEQUENCES = """
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT table_name FROM information_schema.columns
           WHERE table_schema = current_schema() AND column_name = 'id'
  LOOP
    IF pg_get_serial_sequence(format('%I', r.table_name), 'id') IS NOT NULL THEN
      EXECUTE format(
        'SELECT setval(pg_get_serial_sequence(%L, ''id''), COALESCE((SELECT MAX(id) FROM %I), 0) + 1, false)',
        format('%I', r.table_name), r.table_name);
    END IF;
  END LOOP;
END $$;
"""


def sync_sequences(conn: Connection) -> None:
    if conn.dialect.name == "postgresql":
        conn.execute(text(SYNC_SEQUENCES))
