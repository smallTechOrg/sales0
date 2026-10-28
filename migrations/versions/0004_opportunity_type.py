"""Phase 1.5 recall-first triage: ``enquiries.ai_opportunity_type`` (direct_buyer |
agency_requirement | individual_trainer_wanted | other), backfilled from ``ai_extraction``.

Revision ID: 0004_opportunity_type
Revises: p3_outreach_a7c41e9d
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_opportunity_type"
down_revision: str | Sequence[str] | None = "p3_outreach_a7c41e9d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALLOWED = ("direct_buyer", "agency_requirement", "individual_trainer_wanted", "other")


def upgrade() -> None:
    op.add_column("enquiries", sa.Column("ai_opportunity_type", sa.String(length=40), nullable=True))
    bind = op.get_bind()
    enquiries = sa.table(
        "enquiries", sa.column("id", sa.Integer), sa.column("ai_extraction", sa.JSON), sa.column("ai_opportunity_type", sa.String)
    )
    rows = bind.execute(sa.select(enquiries.c.id, enquiries.c.ai_extraction)).all()
    for row_id, extraction in rows:
        value = extraction.get("opportunity_type") if isinstance(extraction, dict) else None
        if value in _ALLOWED:
            bind.execute(enquiries.update().where(enquiries.c.id == row_id).values(ai_opportunity_type=value))


def downgrade() -> None:
    with op.batch_alter_table("enquiries") as batch:
        batch.drop_column("ai_opportunity_type")
