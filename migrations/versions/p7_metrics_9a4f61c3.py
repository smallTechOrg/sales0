"""Phase 4 metrics: ``manual_metrics`` table + indexes for the weekly counters.

Revision ID: p7_metrics_9a4f61c3
Revises: p6_watch_e5b2a7c4
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p7_metrics_9a4f61c3"
down_revision: str | Sequence[str] | None = "p6_watch_e5b2a7c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "manual_metrics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("iso_year", sa.Integer(), nullable=False),
        sa.Column("iso_week", sa.Integer(), nullable=False),
        sa.Column("metric_key", sa.String(30), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(80)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("iso_year", "iso_week", "metric_key", name="uq_manual_metrics_week_metric"),
        sa.CheckConstraint("metric_key IN ('posts_published', 'whatsapp_added')", name="metric_key"),
        sa.CheckConstraint("value >= 0", name="value_non_negative"),
    )
    op.create_index("ix_seen_posts_first_seen_at", "seen_posts", ["first_seen_at"])
    op.create_index("ix_timeline_events_kind_occurred", "timeline_events", ["kind", "occurred_at"])
    op.create_index("ix_outreach_messages_sent_at", "outreach_messages", ["sent_at"])


def downgrade() -> None:
    op.drop_index("ix_outreach_messages_sent_at", table_name="outreach_messages")
    op.drop_index("ix_timeline_events_kind_occurred", table_name="timeline_events")
    op.drop_index("ix_seen_posts_first_seen_at", table_name="seen_posts")
    op.drop_table("manual_metrics")
