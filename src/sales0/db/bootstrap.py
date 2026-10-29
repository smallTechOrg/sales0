"""Hosted-mode database bootstrap: create the database if missing, then ``alembic upgrade head``.

Cloud Run has no place to run ``alembic`` by hand, and the instance is private-IP only, so the single hosted
instance does it at boot (``SALES0_AUTO_MIGRATE=true``). The role may only have CREATEDB (the Cloud SQL
``gcloud sql users create`` default): it creates ``sales0`` itself and so owns it (and its ``public`` schema).
A PostgreSQL advisory lock serialises two instances starting at once (a rollout overlap).
"""

from __future__ import annotations

from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from sales0.db.migrate_check import alembic_config
from sales0.db.pgseq import sync_sequences
from sales0.logging import get_logger

log = get_logger("sales0.db.bootstrap")

_MIGRATION_LOCK_KEY = 0x5A1E50  # arbitrary, stable across releases


def ensure_database(url: str) -> bool:
    """Create the target PostgreSQL database when it does not exist. True when it was created."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not parsed.database:
        return False
    probe = create_engine(url, pool_pre_ping=False)
    try:
        with probe.connect():
            return False
    except OperationalError as exc:
        if "does not exist" not in str(exc):
            raise
    finally:
        probe.dispose()
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            # Serialise two instances starting together (advisory locks are per database: both use `postgres`).
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK_KEY + 1})
            try:
                exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": parsed.database}).scalar()
                if not exists:
                    quoted = '"' + parsed.database.replace('"', '""') + '"'
                    conn.execute(text(f"CREATE DATABASE {quoted}"))
                    log.info("database_created", database=parsed.database)
                    return True
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK_KEY + 1})
    finally:
        admin.dispose()
    return False


def upgrade_to_head(url: str) -> None:
    """``alembic upgrade head`` guarded by an advisory lock (PostgreSQL) so concurrent boots run it once."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        command.upgrade(alembic_config(url), "head")
        return
    # The lock lives on a dedicated connection for the whole upgrade (session-level, released on close).
    guard = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with guard.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK_KEY})
            try:
                command.upgrade(alembic_config(url), "head")
                sync_sequences(conn)  # a seeding migration with explicit ids must not leave a sequence behind
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK_KEY})
    finally:
        guard.dispose()


def bootstrap(url: str) -> None:
    ensure_database(url)
    upgrade_to_head(url)
    log.info("database_at_head")


__all__ = ["bootstrap", "ensure_database", "upgrade_to_head"]
