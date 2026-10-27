"""Alembic helpers: current/head revision and the startup "DB at head" assertion."""

from __future__ import annotations

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from sales0.config import REPO_ROOT

ALEMBIC_INI = REPO_ROOT / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "migrations"


class DatabaseNotAtHead(RuntimeError):
    """The database schema is behind (or ahead of) the migration scripts."""


def alembic_config(database_url: str | None = None) -> Config:
    """An Alembic Config usable from any working directory.

    ``database_url`` overrides the URL from settings; logging config in alembic.ini is
    skipped so the app's structlog setup is never clobbered.
    """
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["configure_logger"] = False
    if database_url:
        cfg.attributes["database_url"] = database_url
    return cfg


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def assert_at_head(engine: Engine | None = None) -> str:
    """Return the current revision, or raise DatabaseNotAtHead with the fix command."""
    if engine is None:
        from sales0.db.engine import get_engine

        engine = get_engine()
    current = current_revision(engine)
    head = head_revision()
    if current != head:
        raise DatabaseNotAtHead(
            f"Database is not at the latest migration (current={current or 'none'}, head={head}). "
            "Run: uv run alembic upgrade head"
        )
    assert current is not None
    return current
