"""Scaffold baseline: the app_meta key/value table.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    app_meta = op.create_table(
        "app_meta",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=500), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_meta")),
    )
    op.bulk_insert(
        app_meta,
        [{"key": "schema_note", "value": "sales0 scaffold baseline (0001_baseline)"}],
    )


def downgrade() -> None:
    op.drop_table("app_meta")
