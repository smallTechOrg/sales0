"""Hosted deployment: the laptop worker. ``workers`` (who polled last) + claim/heartbeat columns on ``scrape_runs``
+ ``scrape_run_keywords.upload_seq`` (idempotent post uploads).

Plain ADD COLUMN / CREATE TABLE only (no table rebuild, so SQLite child rows are never touched), retry-safe: columns
and tables are added only when missing. ``downgrade()`` drops them (SQLite >= 3.35 DROP COLUMN; none is indexed).

Revision ID: p10_worker_e6c2b9d4
Revises: p9_scrapesafe_d3f8a1c6
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p10_worker_e6c2b9d4"
down_revision: str | Sequence[str] | None = "p9_scrapesafe_d3f8a1c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name, SQLite DDL, PostgreSQL DDL): SQLite must say DATETIME / 0 so reflection matches the ORM types.
RUN_COLUMNS = (
    ("worker_name", "VARCHAR(80)", "VARCHAR(80)"),
    ("claimed_at", "DATETIME", "TIMESTAMP WITH TIME ZONE"),
    ("heartbeat_at", "DATETIME", "TIMESTAMP WITH TIME ZONE"),
    ("cancel_requested", "BOOLEAN NOT NULL DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE"),
)


# Rows seeded with explicit ids (offering groups, offerings, the default account...) leave a PostgreSQL SERIAL
# sequence at 1, so the first normal INSERT would collide with them. Move every id sequence past its table's MAX(id).
# Re-runnable; sales0.db.pgseq holds the same statement for boot-time use (a migration never imports app code).
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


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "workers" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "workers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(80), nullable=False),
            sa.Column("version", sa.String(40), nullable=False, server_default=""),
            sa.Column("host", sa.String(120), nullable=False, server_default=""),
            sa.Column("protocol", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("linkedin_status", sa.String(20), nullable=False, server_default="unknown"),
            sa.Column("linkedin_detail", sa.String(300), nullable=True),
            sa.Column("last_run_id", sa.Integer(), nullable=True),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("name", name="uq_workers_name"),
        )
    have = _columns("scrape_runs")
    for name, sqlite_ddl, pg_ddl in RUN_COLUMNS:
        if name not in have:
            op.execute(f"ALTER TABLE scrape_runs ADD COLUMN {name} {sqlite_ddl if bind.dialect.name == 'sqlite' else pg_ddl}")
    if "upload_seq" not in _columns("scrape_run_keywords"):
        op.execute("ALTER TABLE scrape_run_keywords ADD COLUMN upload_seq INTEGER NOT NULL DEFAULT 0")
    if bind.dialect.name == "postgresql":
        op.execute(SYNC_SEQUENCES)


def downgrade() -> None:
    have = _columns("scrape_runs")
    for name, *_ in reversed(RUN_COLUMNS):
        if name in have:
            op.execute(f"ALTER TABLE scrape_runs DROP COLUMN {name}")
    if "upload_seq" in _columns("scrape_run_keywords"):
        op.execute("ALTER TABLE scrape_run_keywords DROP COLUMN upload_seq")
    if "workers" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("workers")
