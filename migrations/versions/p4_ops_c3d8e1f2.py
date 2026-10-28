"""Phase 4 ops: scheduled_jobs, digests, suggestions.

Revision ID: p4_ops_c3d8e1f2
Revises: p3_outreach_a7c41e9d
Create Date: 2026-10-28

Re-chain note: the Phase 1.4 / reply-tracking branches add their own revisions; at merge set
``down_revision`` to the then-current head (``uv run alembic heads``) and keep one chain.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p4_ops_c3d8e1f2"
down_revision: str | Sequence[str] | None = "p3_outreach_a7c41e9d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        "scheduled_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(40), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_started_at", sa.DateTime(timezone=True)),
        sa.Column("last_finished_at", sa.DateTime(timezone=True)),
        sa.Column("last_status", sa.String(10)),
        sa.Column("last_detail", sa.String(500)),
        sa.Column("last_run_id", sa.Integer()),
        sa.Column("runs_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state_json", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "last_status IS NULL OR " + _in("last_status", ("ok", "skipped", "failed", "running", "missed")),
            name="ck_scheduled_jobs_last_status",
        ),
    )
    op.create_table(
        "digests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("digest_date", sa.Date(), nullable=False, unique=True),
        sa.Column("trigger", sa.String(10), nullable=False, server_default="manual"),
        sa.Column("sections_json", sa.Text(), nullable=False),
        sa.Column("summary_text", sa.Text()),
        sa.Column("summary_cost_usd_micros", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gmail_draft_id", sa.String(120)),
        sa.Column("gmail_draft_member_id", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_digests_created_at", "digests", ["created_at"])
    op.create_table(
        "suggestions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("text", sa.String(100), nullable=False),
        sa.Column("keyword_id", sa.Integer()),
        sa.Column("dedupe_key", sa.String(140), nullable=False, unique=True),
        sa.Column("rationale", sa.String(500), nullable=False, server_default=""),
        sa.Column("evidence_json", sa.Text()),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("prompt_version", sa.String(40)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decided_by_member_id", sa.Integer()),
        sa.CheckConstraint(_in("kind", ("new_keyword", "pause_keyword")), name="ck_suggestions_kind"),
        sa.CheckConstraint(_in("status", ("pending", "accepted", "dismissed")), name="ck_suggestions_status"),
    )
    op.create_index("ix_suggestions_status", "suggestions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_suggestions_status", table_name="suggestions")
    op.drop_table("suggestions")
    op.drop_index("ix_digests_created_at", table_name="digests")
    op.drop_table("digests")
    op.drop_table("scheduled_jobs")
