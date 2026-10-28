"""Scrape safety: ``scrape_runs.page_loads`` (LinkedIn page loads a run used, for the per-account daily cap).

A plain ``ALTER TABLE ... ADD COLUMN`` (NOT NULL DEFAULT 0 is allowed by SQLite), retry-safe: the column is
added only when missing. Existing runs get 0 (their page loads were never counted). ``downgrade()`` drops it
(SQLite >= 3.35 ``DROP COLUMN``; the column has no index or constraint).

Revision ID: p9_scrapesafe_d3f8a1c6
Revises: p9_lirekey_7c3e9a15
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p9_scrapesafe_d3f8a1c6"
down_revision: str | Sequence[str] | None = "p9_lirekey_7c3e9a15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("scrape_runs")}
    if "page_loads" not in have:
        op.execute("ALTER TABLE scrape_runs ADD COLUMN page_loads INTEGER NOT NULL DEFAULT 0")


def downgrade() -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("scrape_runs")}
    if "page_loads" in have:
        op.execute("ALTER TABLE scrape_runs DROP COLUMN page_loads")
