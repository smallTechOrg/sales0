"""Prospect watch: watch state columns on linkedin_profiles + the watch_runs marker table.

Revision ID: p6_watch_e5b2a7c4
Revises: p5_replies_c3d81f20
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p6_watch_e5b2a7c4"
down_revision: str | Sequence[str] | None = "p5_replies_c3d81f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("linkedin_profiles") as batch:
        batch.add_column(sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("watch_checks", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("watch_hits", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("watch_last_status", sa.String(20), nullable=True))
        batch.add_column(sa.Column("watch_disabled_until", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "watch_runs",
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("scrape_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("origin", sa.String(12), nullable=False, server_default="manual"),
        sa.Column("hits", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("watch_runs")
    with op.batch_alter_table("linkedin_profiles") as batch:
        for col in ("watch_disabled_until", "watch_last_status", "watch_hits", "watch_checks", "last_checked_at"):
            batch.drop_column(col)
