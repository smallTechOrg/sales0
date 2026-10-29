"""Feedback round 2: follow-up channel plan + email follow-up drafts, enquiry category, prospect priority.

1. ``follow_ups.planned_channel`` (varchar(10), NOT NULL, default ``email``): how the reminder is PLANNED to be done
   (whatsapp / linkedin / email / call). Only ``email`` ones are AI-drafted. Existing rows = ``email``.
2. ``outreach_messages.follow_up_id`` (integer, nullable, indexed): the reminder a drafted follow-up email is for
   (sending it marks THAT reminder done via email). No FK.
3. ``enquiries.ai_category`` (varchar(20), nullable) and ``enquiries.category`` (varchar(20), NOT NULL, default
   ``relevant_prospect``, indexed): relevant_prospect | channel_partner | competitor. Enum validated in the service
   (no CHECK: no table rebuild of the FK-referenced ``enquiries``). Existing rows = ``relevant_prospect``.
4. ``prospects.priority`` (varchar(10), nullable, indexed) and ``prospects.priority_source`` (varchar(10), NOT NULL,
   default ``derived``). BACKFILL: every prospect with linked enquiries gets the highest priority among them
   (high > medium > low), source ``derived``.

Plain ADD COLUMN / CREATE INDEX, retry-safe (each added only when missing; the backfill only fills NULL priorities).
Downgrade drops the columns and indexes.

Revision ID: q16_feedback2_a41c7e92
Revises: q15_research_7d3a9c21
Create Date: 2026-10-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q16_feedback2_a41c7e92"
down_revision: str | Sequence[str] | None = "q15_research_7d3a9c21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RANK = {"high": 3, "medium": 2, "low": 1}


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def _add(table: str, column: sa.Column, index: str | None = None) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)
    if index and index not in _indexes(table):
        op.create_index(index, table, [column.name])


def _backfill_priority() -> None:
    bind = op.get_bind()
    prospects = sa.table("prospects", sa.column("id", sa.Integer), sa.column("priority", sa.String), sa.column("priority_source", sa.String))
    enquiries = sa.table("enquiries", sa.column("prospect_id", sa.Integer), sa.column("priority", sa.String))
    best: dict[int, str] = {}
    for pid, prio in bind.execute(sa.select(enquiries.c.prospect_id, enquiries.c.priority).where(enquiries.c.prospect_id.is_not(None), enquiries.c.priority.is_not(None))):
        if prio in RANK and RANK[prio] > RANK.get(best.get(pid, ""), 0):
            best[pid] = prio
    for pid, prio in best.items():
        bind.execute(
            sa.update(prospects).where(prospects.c.id == pid, prospects.c.priority.is_(None)).values(priority=prio, priority_source="derived")
        )


def upgrade() -> None:
    _add("follow_ups", sa.Column("planned_channel", sa.String(10), nullable=False, server_default="email"))
    _add("outreach_messages", sa.Column("follow_up_id", sa.Integer(), nullable=True), "ix_outreach_messages_follow_up_id")
    _add("enquiries", sa.Column("ai_category", sa.String(20), nullable=True))
    _add("enquiries", sa.Column("category", sa.String(20), nullable=False, server_default="relevant_prospect"), "ix_enquiries_category")
    _add("prospects", sa.Column("priority", sa.String(10), nullable=True), "ix_prospects_priority")
    _add("prospects", sa.Column("priority_source", sa.String(10), nullable=False, server_default="derived"))
    _backfill_priority()


def _drop(table: str, column: str, index: str | None = None) -> None:
    if index and index in _indexes(table):
        op.drop_index(index, table_name=table)
    if column in _columns(table):
        op.drop_column(table, column)


def downgrade() -> None:
    _drop("prospects", "priority_source")
    _drop("prospects", "priority", "ix_prospects_priority")
    _drop("enquiries", "category", "ix_enquiries_category")
    _drop("enquiries", "ai_category")
    _drop("outreach_messages", "follow_up_id", "ix_outreach_messages_follow_up_id")
    _drop("follow_ups", "planned_channel")
