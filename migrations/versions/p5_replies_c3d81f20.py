"""Reply tracking: outreach_replies, suppressions, reply_poll_state, reply columns on outreach_messages.

Revision ID: p5_replies_c3d81f20
Revises: p3_outreach_a7c41e9d
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p5_replies_c3d81f20"
down_revision: str | Sequence[str] | None = "p3_outreach_a7c41e9d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REPLY_KINDS = ("human", "auto_reply", "bounce")
REPLY_INTENTS = ("interested", "not_now", "unsubscribe", "question", "other")
SUPPRESSION_REASONS = ("unsubscribe", "bounce", "manual")


def _in(column: str, values: tuple[str, ...], name: str, nullable: bool = False) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    expr = f"{column} IN ({allowed})"
    if nullable:
        expr = f"{column} IS NULL OR {expr}"
    return sa.CheckConstraint(expr, name=name)


def _ts(name: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    with op.batch_alter_table("outreach_messages") as batch:
        batch.add_column(sa.Column("reply_status", sa.String(12), nullable=False, server_default="none"))
        batch.add_column(_ts("last_reply_at"))
        batch.add_column(sa.Column("follow_up_of_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_outreach_messages_follow_up_of_id_outreach_messages", "outreach_messages", ["follow_up_of_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_index("ix_outreach_messages_follow_up_of_id", ["follow_up_of_id"])

    op.create_table(
        "outreach_replies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("outreach_messages.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("team_members.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("gmail_message_id", sa.String(120), nullable=False, unique=True),
        sa.Column("gmail_thread_id", sa.String(120), nullable=False),
        sa.Column("from_addr", sa.String(254), nullable=False),
        _ts("received_at", nullable=False),
        sa.Column("snippet", sa.String(500), nullable=False, server_default=""),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("intent", sa.String(12), nullable=True),
        sa.Column("reason", sa.String(300), nullable=True),
        sa.Column("classified_by", sa.String(10), nullable=False, server_default="headers"),
        sa.Column("handled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("handled_by_member_id", sa.Integer(), nullable=True),
        _ts("handled_at"),
        _ts("created_at", nullable=False),
        _in("kind", REPLY_KINDS, "kind"),
        _in("intent", REPLY_INTENTS, "intent", nullable=True),
    )
    op.create_index("ix_outreach_replies_handled_received_at", "outreach_replies", ["handled", "received_at"])

    op.create_table(
        "suppressions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("address", sa.String(254), nullable=False, unique=True),
        sa.Column("reason", sa.String(12), nullable=False),
        sa.Column("source_reply_id", sa.Integer(), sa.ForeignKey("outreach_replies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("note", sa.String(300), nullable=True),
        _ts("created_at", nullable=False),
        _in("reason", SUPPRESSION_REASONS, "reason"),
    )

    op.create_table(
        "reply_poll_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("team_members.id", ondelete="CASCADE"), nullable=False, unique=True),
        _ts("last_polled_at"),
        _ts("last_ok_at"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        _ts("next_attempt_at"),
        sa.Column("last_new_replies", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("reply_poll_state")
    op.drop_table("suppressions")
    op.drop_index("ix_outreach_replies_handled_received_at", table_name="outreach_replies")
    op.drop_table("outreach_replies")
    with op.batch_alter_table("outreach_messages") as batch:
        batch.drop_index("ix_outreach_messages_follow_up_of_id")
        batch.drop_constraint("fk_outreach_messages_follow_up_of_id_outreach_messages", type_="foreignkey")
        batch.drop_column("follow_up_of_id")
        batch.drop_column("last_reply_at")
        batch.drop_column("reply_status")
