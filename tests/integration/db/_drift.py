"""Schema-drift helper shared by the migration tests.

SQLite cannot add a constraint to an existing table, so migration 0003 adds the three new
foreign-key columns with ``ALTER TABLE ... ADD COLUMN ... REFERENCES t(id) ON DELETE SET NULL``
(no table rebuild: ``enquiries`` is FK-referenced). SQLAlchemy cannot reflect the ``ON DELETE``
option of such an inline reference and Alembic reports a spurious remove/add pair. Those pairs
are dropped here; ``assert_inline_fks_cascade`` proves the real rule via ``PRAGMA foreign_key_list``.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from sales0.db.base import metadata

INLINE_FKS = {
    ("enquiries", "author_profile_id"),
    ("enquiries", "seen_post_id"),
    ("llm_calls", "seen_post_id"),
    # p6_crm_5b2e7d14: the CRM links on enquiries are added the same way (enquiries is FK-referenced).
    ("enquiries", "prospect_id"),
    ("enquiries", "company_id"),
    ("enquiries", "contact_id"),
    # p8_liaccts_b2e5f7a9: scrape_runs is FK-referenced, so its account link is added the same way.
    ("scrape_runs", "linkedin_account_id"),
}


def _is_inline_fk_noise(diff: Any) -> bool:
    if not isinstance(diff, tuple) or diff[0] not in ("add_fk", "remove_fk"):
        return False
    fk = diff[1]
    return (
        len(fk.elements) == 1
        and (fk.table.name, fk.elements[0].parent.name) in INLINE_FKS
    )


def drift(engine: sa.Engine) -> list[Any]:
    with engine.connect() as conn:
        diffs = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True}), metadata
        )
    return [d for d in diffs if not _is_inline_fk_noise(d)]


def assert_inline_fks_cascade(engine: sa.Engine) -> None:
    if engine.dialect.name != "sqlite":  # PostgreSQL reflects ON DELETE directly; the inline-FK blind spot is SQLite's
        insp = sa.inspect(engine)
        for table, column in INLINE_FKS:
            mine = [fk for fk in insp.get_foreign_keys(table) if fk["constrained_columns"] == [column]]
            assert len(mine) == 1, (table, column)
            assert (mine[0].get("options") or {}).get("ondelete") == "SET NULL", (table, column, mine[0])
        return
    with engine.connect() as conn:
        for table, column in INLINE_FKS:
            rows = (
                conn.execute(sa.text(f"PRAGMA foreign_key_list({table})"))
                .mappings()
                .all()
            )
            mine = [r for r in rows if r["from"] == column]
            assert len(mine) == 1, (table, column)
            assert mine[0]["on_delete"] == "SET NULL", (table, column, dict(mine[0]))
