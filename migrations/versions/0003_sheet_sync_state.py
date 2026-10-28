"""Sheet-sync bookkeeping table (last run + per-tab report JSON).

Revision ID: 0003_sheet_sync_state
Revises: 0002_phase1_discovery
Create Date: 2026-10-28

NOTE for the Phase-2 integrator: this revision was written on the sheet-sync branch against
head ``0002_phase1_discovery``; re-chain ``down_revision`` at merge.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_sheet_sync_state"
down_revision: str | Sequence[str] | None = "0002_phase1_discovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = ("ok", "partial", "failed")


def upgrade() -> None:
    allowed = ", ".join(f"'{s}'" for s in STATUSES)
    op.create_table(
        "sheet_sync_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(10), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("last_dry_run", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("last_duration_s", sa.Float(), nullable=True),
        sa.Column("last_report_json", sa.Text(), nullable=True),
        sa.Column("runs_total", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("id = 1", name="ck_sheet_sync_state_singleton"),
        sa.CheckConstraint(f"last_status IS NULL OR last_status IN ({allowed})", name="ck_sheet_sync_state_last_status"),
    )


def downgrade() -> None:
    op.drop_table("sheet_sync_state")
