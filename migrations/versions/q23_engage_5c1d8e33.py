"""q23: company offerings (company_offerings), reminders, the free-text follow-up log (follow_up_notes), long text
settings (text_settings, the drafting instructions) and the "Direct client" characteristic.

Revision ID: q23_engage_5c1d8e33
Revises: q22_criteria_offering_9b4e2a17
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "q23_engage_5c1d8e33"
down_revision: str | Sequence[str] | None = "q22_criteria_offering_9b4e2a17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "company_offerings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("offering_id", sa.Integer(), sa.ForeignKey("offerings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(10), nullable=False, server_default="manual"),
        _ts("created_at"),
        sa.UniqueConstraint("company_id", "offering_id", name="uq_company_offerings"),
    )
    op.create_index("ix_company_offerings_company_id", "company_offerings", ["company_id"])
    op.create_index("ix_company_offerings_offering_id", "company_offerings", ["offering_id"])
    op.create_table(
        "reminders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_type", sa.String(20), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=True),
        sa.Column("prospect_id", sa.Integer(), sa.ForeignKey("prospects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("note", sa.String(300), nullable=False, server_default=""),
        sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("created_by", sa.String(80), nullable=False, server_default="system"),
        sa.Column("owner_member_id", sa.Integer(), sa.ForeignKey("team_members.id", ondelete="SET NULL"), nullable=True),
        _ts("done_at", True),
        sa.Column("done_by", sa.String(80), nullable=True),
        _ts("cancelled_at", True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_reminders_open_due", "reminders", ["done_at", "cancelled_at", "due_on"])
    op.create_index("ix_reminders_subject", "reminders", ["subject_type", "subject_id"])
    op.create_index("ix_reminders_company_id", "reminders", ["company_id"])
    op.create_index("ix_reminders_prospect_id", "reminders", ["prospect_id"])
    op.create_table(
        "follow_up_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("prospect_id", sa.Integer(), sa.ForeignKey("prospects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("how", sa.String(20), nullable=False, server_default="other"),
        sa.Column("notes", sa.String(1000), nullable=False, server_default=""),
        sa.Column("happened_on", sa.Date(), nullable=False),
        sa.Column("contact_id", sa.Integer(), sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("contact_name", sa.String(200), nullable=True),
        sa.Column("reminder_id", sa.Integer(), sa.ForeignKey("reminders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("structured_by", sa.String(10), nullable=False, server_default="ai"),
        sa.Column("actor", sa.String(80), nullable=False, server_default="team"),
        sa.Column("actor_member_id", sa.Integer(), sa.ForeignKey("team_members.id", ondelete="SET NULL"), nullable=True),
        _ts("created_at"),
    )
    op.create_index("ix_follow_up_notes_prospect_id", "follow_up_notes", ["prospect_id"])
    op.create_index("ix_follow_up_notes_company_id", "follow_up_notes", ["company_id"])
    op.create_table(
        "text_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(80), nullable=True),
        _ts("updated_at"),
    )
    bind = op.get_bind()
    chars = sa.table("characteristics", sa.column("id", sa.Integer), sa.column("name", sa.String), sa.column("name_key", sa.String),
                     sa.column("system_key", sa.String), sa.column("active", sa.Boolean), sa.column("sort_order", sa.Integer),
                     sa.column("created_at", sa.DateTime), sa.column("updated_at", sa.DateTime))
    have = bind.execute(sa.select(chars.c.id).where((chars.c.system_key == "direct_client") | (chars.c.name_key == "direct client"))).first()
    if have is None:
        now = datetime.now(UTC).replace(tzinfo=None)
        top = bind.execute(sa.select(sa.func.max(chars.c.sort_order))).scalar() or 0
        bind.execute(chars.insert().values(name="Direct client", name_key="direct client", system_key="direct_client", active=True,
                                           sort_order=int(top) + 1, created_at=now, updated_at=now))
    else:
        bind.execute(chars.update().where(chars.c.id == have[0]).values(system_key="direct_client"))


def downgrade() -> None:
    op.drop_table("text_settings")
    op.drop_table("follow_up_notes")
    op.drop_table("reminders")
    op.drop_table("company_offerings")
