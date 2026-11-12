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
    # Plain ALTER TABLE ... DROP COLUMN on every dialect (SQLite >= 3.35 supports it): never a batch table rebuild, which
    # on SQLite would drop + recreate ``companies`` and fire the ON DELETE SET NULL of every table pointing at it.
    for col in COLUMNS:
        op.execute(f"ALTER TABLE companies DROP COLUMN {col}")


def downgrade() -> None:
    op.add_column("companies", sa.Column("status_override_at", sa.DateTime(timezone=True)))
    op.add_column("companies", sa.Column("status_override_by", sa.String(80)))
    op.add_column("companies", sa.Column("status_override_stage", sa.String(10)))
    op.add_column("companies", sa.Column("status_override_sub", sa.String(20)))
