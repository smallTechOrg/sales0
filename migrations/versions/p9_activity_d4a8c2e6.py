"""``enquiries.activity_id``: the LinkedIn activity id as an indexed column (no more ``LIKE '%id%'`` scans).

The id (digits after ``activity-`` / ``activity:`` / ``ugcPost-`` / ``share-``...) used to be found by scanning
``enquiries.source_url`` and ``enquiry_sources.source_ref/source_url`` with ``LIKE``; it is now stored once.
``enquiries`` is FK-referenced, so the column is added with a plain ``ALTER TABLE ... ADD COLUMN`` (no rebuild).
``upgrade()`` is retry-safe (column/index only when missing) and backfills every row that has a reference:
the enquiry's own ``source_url`` first, then its sources oldest first. Never imports app code.

Revision ID: p9_activity_d4a8c2e6
Revises: p9_reply_feat_c1e7a3b5
Create Date: 2026-10-28
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p9_activity_d4a8c2e6"
down_revision: str | Sequence[str] | None = "p9_reply_feat_c1e7a3b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_enquiries_activity_id"
# FROZEN copy of services.dedupe.ACTIVITY_RE.
ACTIVITY_RE = re.compile(r"(?:activity|ugcPost|share)[-:](\d{6,})", re.IGNORECASE)


def _first_id(*texts: str | None) -> str | None:
    for t in texts:
        if t:
            m = ACTIVITY_RE.search(t)
            if m:
                return m.group(1)
    return None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "activity_id" not in {c["name"] for c in insp.get_columns("enquiries")}:
        op.execute("ALTER TABLE enquiries ADD COLUMN activity_id VARCHAR(64)")
    if INDEX not in {i["name"] for i in sa.inspect(bind).get_indexes("enquiries")}:
        op.create_index(INDEX, "enquiries", ["activity_id"])
    sources: dict[int, list[str | None]] = {}
    for eid, ref, url in bind.execute(sa.text("SELECT enquiry_id, source_ref, source_url FROM enquiry_sources ORDER BY id")):
        sources.setdefault(int(eid), []).extend([url, ref])
    rows = bind.execute(sa.text("SELECT id, source_url FROM enquiries WHERE activity_id IS NULL")).fetchall()
    for eid, url in rows:
        found = _first_id(url, *sources.get(int(eid), []))
        if found:
            bind.execute(sa.text("UPDATE enquiries SET activity_id = :a WHERE id = :i"), {"a": found, "i": eid})


def downgrade() -> None:
    bind = op.get_bind()
    if INDEX in {i["name"] for i in sa.inspect(bind).get_indexes("enquiries")}:
        op.drop_index(INDEX, table_name="enquiries")
    if bind.dialect.name == "sqlite":
        # DROP COLUMN needs SQLite >= 3.35 and an un-indexed column (the index is gone above).
        op.execute("ALTER TABLE enquiries DROP COLUMN activity_id")
    else:
        op.drop_column("enquiries", "activity_id")
