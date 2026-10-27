"""Phase 1 discovery schema: users, offerings catalogue, keywords, LinkedIn session,
scrape runs, enquiries (+ sources, keyword hits), triage decisions, LLM calls, status
history. Seeds the 3 offering groups, the 4 default offerings and the session singleton.

Revision ID: 0002_phase1_discovery
Revises: 0001_baseline
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0002_phase1_discovery"
down_revision: str | Sequence[str] | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of the enum values at this revision (never import app code here).
LINKEDIN_SESSION_STATUSES = ("not_connected", "connected", "needs_attention")
RUN_TRIGGERS = ("manual",)
RUN_MODES = ("live", "fixture")
RUN_STATUSES = ("queued", "running", "succeeded", "aborted", "failed")
ABORT_REASONS = ("logout", "checkpoint", "captcha", "rate_limited", "daily_post_cap")
RUN_KEYWORD_STATUSES = ("pending", "running", "done", "warning", "error", "skipped")
ENQUIRY_SOURCES = ("linkedin_post",)
REVIEW_STATUSES = ("pending_review", "accepted", "discarded")
ENQUIRY_STAGES = ("prospect_new",)
TRIAGE_STATUSES = ("pending", "done", "failed")
VERDICTS = ("genuine", "discard")
PRIORITIES = ("high", "medium", "low")
ENGAGEMENT_MODES = ("online", "offline", "hybrid", "unknown")
DECISIONS = ("accept", "discard")
LLM_FEATURES = ("ping", "triage")
LLM_CALL_STATUSES = ("ok", "error")
HISTORY_ENTITY_TYPES = ("enquiry", "scrape_run", "linkedin_session")

OFFERING_GROUPS = [
    {"id": 1, "name": "Training", "sort_order": 1},
    {"id": 2, "name": "Services", "sort_order": 2},
    {"id": 3, "name": "Products", "sort_order": 3},
]

OFFERINGS = [
    {
        "id": 1,
        "group_id": 1,
        "name": "AI corporate workshops",
        "description": (
            "Instructor-led AI/GenAI awareness and productivity workshops for company teams, managers "
            "and leadership, online or on-site."
        ),
        "sort_order": 1,
    },
    {
        "id": 2,
        "group_id": 1,
        "name": "Technical AI training",
        "description": (
            'Hands-on technical programs for engineers, e.g. "Build your first AI agent", Claude Code, '
            "LLM app development."
        ),
        "sort_order": 2,
    },
    {
        "id": 3,
        "group_id": 2,
        "name": "AI consulting & build services",
        "description": (
            "Consulting, design and build of AI solutions: assistants/chatbots, agents, automation, RAG "
            "over company documents."
        ),
        "sort_order": 3,
    },
    {
        "id": 4,
        "group_id": 3,
        "name": "Products",
        "description": "smallTech's AI products and tools offered to companies.",
        "sort_order": 4,
    },
]


def _in(column: str, values: tuple[str, ...], name: str, *, nullable: bool = False) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    expr = f"{column} IN ({allowed})"
    if nullable:
        expr = f"{column} IS NULL OR {expr}"
    return sa.CheckConstraint(expr, name=name)


def _ts(name: str, *, nullable: bool = True, index: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, index=index)


def _counter(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default="0")


def _actor() -> sa.Column:
    return sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("display_name", sa.String(80), nullable=False),
        sa.Column("email", sa.String(254), nullable=True, unique=True),
        _ts("created_at", nullable=False),
    )

    offering_groups = op.create_table(
        "offering_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("name_key", sa.String(60), nullable=False, unique=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        _ts("created_at", nullable=False),
    )

    offerings = op.create_table(
        "offerings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("offering_groups.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("name_key", sa.String(80), nullable=False, unique=True),
        sa.Column("description", sa.String(500), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
    )

    op.create_table(
        "keywords",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("text", sa.String(100), nullable=False),
        sa.Column("text_key", sa.String(100), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        _ts("last_run_at"),
        _ts("created_at", nullable=False),
        _actor(),
    )

    linkedin_session = op.create_table(
        "linkedin_session",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="not_connected"),
        _ts("last_checked_at"),
        _ts("last_connected_at"),
        _ts("last_successful_run_at"),
        sa.Column("last_error", sa.String(300), nullable=True),
        _ts("updated_at", nullable=False),
        sa.CheckConstraint("id = 1", name="singleton"),
        _in("status", LINKEDIN_SESSION_STATUSES, "status"),
    )

    op.create_table(
        "scrape_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trigger", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        _ts("started_at", index=True),
        _ts("finished_at"),
        _counter("keywords_planned"),
        _counter("keywords_done"),
        sa.Column("current_keyword", sa.String(100), nullable=True),
        sa.Column("current_step", sa.String(200), nullable=True),
        _counter("posts_seen"),
        _counter("posts_new"),
        _counter("posts_dupe"),
        _counter("enquiries_triaged"),
        _counter("triage_failed"),
        _counter("errors_count"),
        sa.Column("abort_reason", sa.String(20), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        _actor(),
        _in("trigger", RUN_TRIGGERS, "trigger"),
        _in("mode", RUN_MODES, "mode"),
        _in("status", RUN_STATUSES, "status"),
        _in("abort_reason", ABORT_REASONS, "abort_reason", nullable=True),
    )

    op.create_table(
        "scrape_run_keywords",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id", sa.Integer(), sa.ForeignKey("scrape_runs.id", ondelete="CASCADE"), nullable=False, index=True
        ),
        sa.Column("keyword_id", sa.Integer(), sa.ForeignKey("keywords.id", ondelete="SET NULL"), nullable=True),
        sa.Column("keyword_text", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        _counter("posts_seen"),
        _counter("posts_new"),
        _counter("posts_dupe"),
        _counter("errors"),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.String(300), nullable=True),
        _in("status", RUN_KEYWORD_STATUSES, "status"),
    )

    op.create_table(
        "enquiries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(20), nullable=False, server_default="linkedin_post"),
        sa.Column("content_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("author_name", sa.String(200), nullable=True),
        sa.Column("author_headline", sa.String(500), nullable=True),
        sa.Column("author_profile_url", sa.String(1000), nullable=True),
        sa.Column("source_url", sa.String(1000), nullable=True),
        sa.Column("date_posted", sa.Date(), nullable=True),
        _ts("date_added", nullable=False, index=True),
        sa.Column("review_status", sa.String(20), nullable=False, server_default="pending_review"),
        sa.Column("stage", sa.String(30), nullable=True),
        sa.Column("triage_status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("triage_error", sa.String(300), nullable=True),
        _ts("triaged_at"),
        sa.Column("prompt_version", sa.String(40), nullable=True),
        sa.Column("ai_verdict", sa.String(10), nullable=True),
        sa.Column("ai_reason", sa.String(160), nullable=True),
        sa.Column("ai_poster_type", sa.String(40), nullable=True),
        sa.Column("ai_priority", sa.String(10), nullable=True),
        sa.Column(
            "ai_offering_id", sa.Integer(), sa.ForeignKey("offerings.id", ondelete="RESTRICT"), nullable=True
        ),
        sa.Column("ai_extraction", sa.JSON(), nullable=True),
        sa.Column("requirement_summary", sa.String(140), nullable=True),
        sa.Column("priority", sa.String(10), nullable=True),
        sa.Column("offering_id", sa.Integer(), sa.ForeignKey("offerings.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("company", sa.String(200), nullable=True, index=True),
        sa.Column("contact_person", sa.String(200), nullable=True),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("city", sa.String(100), nullable=True),
        sa.Column("mode", sa.String(10), nullable=True),
        sa.Column("dates_mentioned", sa.String(300), nullable=True),
        sa.Column("topic", sa.String(300), nullable=True),
        sa.Column("discard_reason", sa.String(200), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("update_note", sa.Text(), nullable=True),
        sa.Column("date_email_sent", sa.Date(), nullable=True),
        sa.Column("date_proposal_sent", sa.Date(), nullable=True),
        _ts("decided_at"),
        _actor(),
        sa.Index("ix_enquiries_inbox", "review_status", "ai_verdict", "priority", "date_added"),
        _in("source", ENQUIRY_SOURCES, "source"),
        _in("review_status", REVIEW_STATUSES, "review_status"),
        _in("stage", ENQUIRY_STAGES, "stage", nullable=True),
        _in("triage_status", TRIAGE_STATUSES, "triage_status"),
        _in("ai_verdict", VERDICTS, "ai_verdict", nullable=True),
        _in("priority", PRIORITIES, "priority", nullable=True),
        _in("mode", ENGAGEMENT_MODES, "mode", nullable=True),
    )

    op.create_table(
        "enquiry_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "enquiry_id", sa.Integer(), sa.ForeignKey("enquiries.id", ondelete="CASCADE"), nullable=False, index=True
        ),
        sa.Column("source_ref", sa.String(200), nullable=False, unique=True),
        sa.Column("source_url", sa.String(1000), nullable=True),
        sa.Column(
            "first_seen_run_id", sa.Integer(), sa.ForeignKey("scrape_runs.id", ondelete="SET NULL"), nullable=True
        ),
        _ts("first_seen_at", nullable=False),
    )

    op.create_table(
        "enquiry_keyword_hits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "enquiry_id", sa.Integer(), sa.ForeignKey("enquiries.id", ondelete="CASCADE"), nullable=False, index=True
        ),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("scrape_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("keyword_id", sa.Integer(), sa.ForeignKey("keywords.id", ondelete="SET NULL"), nullable=True),
        sa.Column("keyword_text", sa.String(100), nullable=False),
        _ts("seen_at", nullable=False),
        sa.UniqueConstraint("enquiry_id", "run_id", "keyword_text"),
    )

    op.create_table(
        "triage_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "enquiry_id", sa.Integer(), sa.ForeignKey("enquiries.id", ondelete="CASCADE"), nullable=False, index=True
        ),
        sa.Column("decision", sa.String(10), nullable=False),
        sa.Column("ai_verdict", sa.String(10), nullable=True),
        sa.Column("is_override", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(200), nullable=True),
        sa.Column("field_overrides", sa.JSON(), nullable=True),
        _ts("decided_at", nullable=False, index=True),
        _actor(),
        _in("decision", DECISIONS, "decision"),
        _in("ai_verdict", VERDICTS, "ai_verdict", nullable=True),
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("feature", sa.String(20), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(40), nullable=True),
        sa.Column(
            "enquiry_id", sa.Integer(), sa.ForeignKey("enquiries.id", ondelete="SET NULL"), nullable=True, index=True
        ),
        sa.Column(
            "run_id", sa.Integer(), sa.ForeignKey("scrape_runs.id", ondelete="SET NULL"), nullable=True, index=True
        ),
        _counter("input_tokens"),
        _counter("output_tokens"),
        _counter("cost_usd_micros"),
        _counter("latency_ms"),
        _counter("fewshot_count"),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("error_type", sa.String(100), nullable=True),
        sa.Column("request_id", sa.String(100), nullable=True),
        _ts("created_at", nullable=False, index=True),
        _in("feature", LLM_FEATURES, "feature"),
        _in("status", LLM_CALL_STATUSES, "status"),
    )

    op.create_table(
        "status_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_type", sa.String(30), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("field", sa.String(40), nullable=False),
        sa.Column("from_value", sa.String(40), nullable=True),
        sa.Column("to_value", sa.String(40), nullable=True),
        sa.Column("note", sa.String(500), nullable=True),
        _ts("changed_at", nullable=False),
        _actor(),
        sa.Index("ix_status_history_entity", "entity_type", "entity_id", "changed_at"),
        _in("entity_type", HISTORY_ENTITY_TYPES, "entity_type"),
    )

    # --- seeds ----------------------------------------------------------------------
    # SQLite stores DateTime without an offset; the ORM type re-attaches UTC on load.
    now = datetime.now(UTC)
    stamp = now.replace(tzinfo=None) if op.get_bind().dialect.name == "sqlite" else now

    op.bulk_insert(
        offering_groups,
        [{**g, "name_key": g["name"].strip().lower(), "created_at": stamp} for g in OFFERING_GROUPS],
    )
    op.bulk_insert(
        offerings,
        [
            {**o, "name_key": o["name"].strip().lower(), "active": True, "created_at": stamp, "updated_at": stamp}
            for o in OFFERINGS
        ],
    )
    op.bulk_insert(linkedin_session, [{"id": 1, "status": "not_connected", "updated_at": stamp}])


def downgrade() -> None:
    for table in (
        "status_history",
        "llm_calls",
        "triage_decisions",
        "enquiry_keyword_hits",
        "enquiry_sources",
        "enquiries",
        "scrape_run_keywords",
        "scrape_runs",
        "linkedin_session",
        "keywords",
        "offerings",
        "offering_groups",
        "users",
    ):
        op.drop_table(table)
