"""Criteria per offering: ``triage_criteria.offering_id`` (nullable FK to offerings, SET NULL). A rule with an
offering is rendered as "(only for offering X)" and skipped while that offering is off; NULL = applies to every post.

Revision ID: q22_criteria_offering_9b4e2a17
Revises: q21_layout_samples_7e2b4c90
Create Date: 2026-10-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q22_criteria_offering_9b4e2a17"
down_revision: str | Sequence[str] | None = "q21_layout_samples_7e2b4c90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("triage_criteria") as batch:
        batch.add_column(sa.Column("offering_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_triage_criteria_offering_id", "offerings", ["offering_id"], ["id"], ondelete="SET NULL")
        batch.create_index("ix_triage_criteria_offering_id", ["offering_id"])


def downgrade() -> None:
    with op.batch_alter_table("triage_criteria") as batch:
        batch.drop_index("ix_triage_criteria_offering_id")
        batch.drop_constraint("fk_triage_criteria_offering_id", type_="foreignkey")
        batch.drop_column("offering_id")
