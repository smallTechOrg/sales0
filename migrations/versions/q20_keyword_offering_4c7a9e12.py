"""Tie keywords to an offering (keywords.offering_id), backfilled from the group name.

Keywords were grouped under an offering by matching ``group_name`` to the offering name. The link is now a
real foreign key so toggling an offering off pauses its keywords in runs.

Revision ID: q20_keyword_offering_4c7a9e12
Revises: q19_priority_heal_d3b81f40
Create Date: 2026-10-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q20_keyword_offering_4c7a9e12"
down_revision: str | Sequence[str] | None = "q19_priority_heal_d3b81f40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("keywords") as batch:
        batch.add_column(sa.Column("offering_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_keywords_offering_id", "offerings", ["offering_id"], ["id"], ondelete="SET NULL")
        batch.create_index("ix_keywords_offering_id", ["offering_id"])
    bind = op.get_bind()
    offerings = sa.table("offerings", sa.column("id", sa.Integer), sa.column("name", sa.String))
    keywords = sa.table("keywords", sa.column("id", sa.Integer), sa.column("group_name", sa.String), sa.column("offering_id", sa.Integer))
    by_name = {str(n).strip().lower(): int(i) for i, n in bind.execute(sa.select(offerings.c.id, offerings.c.name))}
    for kid, group in bind.execute(sa.select(keywords.c.id, keywords.c.group_name)).all():
        oid = by_name.get((group or "").strip().lower())
        if oid is not None:
            bind.execute(keywords.update().where(keywords.c.id == kid).values(offering_id=oid))


def downgrade() -> None:
    with op.batch_alter_table("keywords") as batch:
        batch.drop_index("ix_keywords_offering_id")
        batch.drop_constraint("fk_keywords_offering_id", type_="foreignkey")
        batch.drop_column("offering_id")
