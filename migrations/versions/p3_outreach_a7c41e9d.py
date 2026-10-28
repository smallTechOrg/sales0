"""Phase 3 outreach schema: team members, per-member Gmail connections, message templates,
decks, outreach messages (+ append-only events) and company briefs.

CRM references (prospect_id / enquiry_id / company_id / offering_id) are plain integers with
NO foreign keys: the CRM tables do not exist on this branch (the integrator adds FKs at merge).

Revision ID: p3_outreach_a7c41e9d
Revises: 0003_sheet_sync_state
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p3_outreach_a7c41e9d"
down_revision: str | Sequence[str] | None = "0003_sheet_sync_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of the enum values at this revision (never import app code here).
MEMBER_ROLES = ("admin", "member")
GMAIL_STATUSES = ("connected", "needs_reauth", "scope_insufficient", "disconnected")
LLM_FEATURES_OLD = ("ping", "triage")
LLM_FEATURES_NEW = ("ping", "triage", "research", "drafting", "suggest")
OUTREACH_STATES = ("draft", "edited", "approved", "sending", "sent", "failed", "rejected")


def _in(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({allowed})", name=name)


def _ts(name: str, *, nullable: bool = True, index: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, index=index)


def _set_llm_features(values: tuple[str, ...]) -> None:
    """Recreate llm_calls' feature CHECK with ``values`` (SQLite needs a table recreate)."""
    # Phase 1.4 added seen_post_id via ALTER TABLE ... ADD COLUMN ... REFERENCES ... ON DELETE SET NULL;
    # SQLite reflection loses that inline ON DELETE, so restate it or the recreate would downgrade it
    # to NO ACTION. (No-op on a DB where the column already reflects correctly.)
    seen_post_fk = sa.Column(
        "seen_post_id", sa.Integer(), sa.ForeignKey("seen_posts.id", ondelete="SET NULL")
    )
    with op.batch_alter_table("llm_calls", reflect_args=[seen_post_fk]) as batch:
        batch.drop_constraint(op.f("ck_llm_calls_feature"), type_="check")
        batch.create_check_constraint("feature", _in("feature", values, "feature").sqltext)


def upgrade() -> None:
    _set_llm_features(LLM_FEATURES_NEW)
    op.create_table(
        "team_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("role", sa.String(10), nullable=False, server_default="member"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        _ts("created_at", nullable=False),
        _in("role", MEMBER_ROLES, "role"),
    )

    op.create_table(
        "gmail_connections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "member_id", sa.Integer(), sa.ForeignKey("team_members.id", ondelete="CASCADE"), nullable=False, unique=True
        ),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=True),
        sa.Column("token_encrypted", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="connected"),
        _ts("connected_at"),
        _ts("updated_at", nullable=False),
        sa.Column("last_error", sa.String(300), nullable=True),
        _in("status", GMAIL_STATUSES, "status"),
    )

    op.create_table(
        "message_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("offering_id", sa.Integer(), nullable=True, index=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("subject", sa.String(300), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
    )

    op.create_table(
        "decks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("offering_id", sa.Integer(), nullable=True, index=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("stored_path", sa.String(500), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        _ts("created_at", nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    op.create_table(
        "outreach_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "member_id", sa.Integer(), sa.ForeignKey("team_members.id", ondelete="RESTRICT"), nullable=False, index=True
        ),
        sa.Column("connection_email", sa.String(254), nullable=False),
        sa.Column("to_addrs", sa.Text(), nullable=False, server_default=""),
        sa.Column("cc_addrs", sa.Text(), nullable=False, server_default=""),
        sa.Column("from_addr", sa.String(254), nullable=False),
        sa.Column("subject", sa.String(300), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("template_id", sa.Integer(), nullable=True),
        sa.Column("deck_id", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(10), nullable=False, server_default="draft", index=True),
        sa.Column("content_sha256", sa.String(64), nullable=True),
        sa.Column(
            "approved_by_member_id", sa.Integer(), sa.ForeignKey("team_members.id", ondelete="SET NULL"), nullable=True
        ),
        _ts("approved_at"),
        sa.Column("gmail_draft_id", sa.String(120), nullable=True),
        sa.Column("gmail_message_id", sa.String(120), nullable=True),
        sa.Column("gmail_thread_id", sa.String(120), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("send_outcome_unknown", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("retry_decided_by", sa.String(80), nullable=True),
        sa.Column("prospect_id", sa.Integer(), nullable=True, index=True),
        sa.Column("enquiry_id", sa.Integer(), nullable=True, index=True),
        sa.Column("company_id", sa.Integer(), nullable=True, index=True),
        sa.Column("brief_json", sa.Text(), nullable=True),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        _ts("sent_at"),
        _in("state", OUTREACH_STATES, "state"),
    )

    op.create_table(
        "outreach_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "message_id", sa.Integer(), sa.ForeignKey("outreach_messages.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        _ts("created_at", nullable=False),
    )
    op.create_index("ix_outreach_events_message_id_created_at", "outreach_events", ["message_id", "created_at"])

    op.create_table(
        "company_briefs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_name", sa.String(200), nullable=False),
        sa.Column("website", sa.String(300), nullable=True),
        sa.Column("brief_json", sa.Text(), nullable=False),
        sa.Column("sources_json", sa.Text(), nullable=True),
        _ts("created_at", nullable=False, index=True),
        sa.Column("cost_usd_micros", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enquiry_id", sa.Integer(), nullable=True, index=True),
        sa.Column("prospect_id", sa.Integer(), nullable=True, index=True),
    )


def downgrade() -> None:
    op.execute("DELETE FROM llm_calls WHERE feature NOT IN ('ping', 'triage')")
    _set_llm_features(LLM_FEATURES_OLD)
    for table in (
        "company_briefs",
        "outreach_events",
        "outreach_messages",
        "decks",
        "message_templates",
        "gmail_connections",
        "team_members",
    ):
        op.drop_table(table)
