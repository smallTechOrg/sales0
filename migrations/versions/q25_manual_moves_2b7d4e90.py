"""q25: no automatic status - drop the manual-override columns of q24 (companies only move when a person moves them).

Revision ID: q25_manual_moves_2b7d4e90
Revises: q24_auto_status_8d2f6b41
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q25_manual_moves_2b7d4e90"
down_revision: str | Sequence[str] | None = "q24_auto_status_8d2f6b41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("status_override_sub", "status_override_stage", "status_override_by", "status_override_at")


def upgrade() -> None:
    with op.batch_alter_table("companies") as batch:
        for col in COLUMNS:
            batch.drop_column(col)


def downgrade() -> None:
    with op.batch_alter_table("companies") as batch:
        batch.add_column(sa.Column("status_override_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("status_override_by", sa.String(80)))
        batch.add_column(sa.Column("status_override_stage", sa.String(10)))
        batch.add_column(sa.Column("status_override_sub", sa.String(20)))
