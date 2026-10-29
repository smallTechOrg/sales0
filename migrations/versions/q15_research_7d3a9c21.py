"""Prospect research + account summary (versioned), our own notes, laptop-worker tasks, web-search counts.

1. ``prospect_docs``: every research run, account-summary refresh and human edit of either is a new version
   (``version`` per prospect + ``doc_type``; the highest is current). BACKFILL: every ``company_briefs`` row that
   names a prospect becomes a ``research`` version (origin ai, oldest first, ``company_brief_id`` kept).
2. ``prospect_notes``: dated, authored notes. BACKFILL: a non-empty ``prospects.notes`` becomes the first note
   (source ``migrated``, author "team", dated at the prospect's ``updated_at``). The column itself is kept as a
   legacy, read-only value (the app no longer writes it), so a downgrade loses nothing.
3. ``worker_tasks``: one-page LinkedIn tasks for the laptop worker (company page text).
4. ``llm_calls.web_search_requests`` (server-side web searches, billed per search) and the ``feature`` CHECK widened
   with ``'summary'``. SQLite cannot alter a CHECK: ``llm_calls`` (a leaf table) is rebuilt with the four-step recipe
   exactly like ``p9_reply_feat_c1e7a3b5``.

Plain CREATE TABLE / ADD COLUMN, retry-safe (only what is missing; the backfills skip what is already there).
Downgrade drops the new tables / column, maps ``summary`` llm_calls rows to ``research`` (spend history is kept) and
restores the narrower CHECK.

Revision ID: q15_research_7d3a9c21
Revises: q14_criteria_5b8e1f3a
Create Date: 2026-10-29
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "q15_research_7d3a9c21"
down_revision: str | Sequence[str] | None = "q14_criteria_5b8e1f3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURES_OLD = ("ping", "triage", "research", "drafting", "suggest", "reply")
FEATURES_NEW = (*FEATURES_OLD, "summary")


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _stamp(value: object = None) -> datetime:
    stamp = value if isinstance(value, datetime) else datetime.now(UTC)
    if op.get_bind().dialect.name == "sqlite":
        return stamp.replace(tzinfo=None)
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------- llm_calls feature CHECK
def _check_sql(values: tuple[str, ...]) -> str:
    return "feature IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _set_features(values: tuple[str, ...]) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.drop_constraint(op.f("ck_llm_calls_feature"), "llm_calls", type_="check")
        op.create_check_constraint("feature", "llm_calls", sa.text(_check_sql(values)))
        return
    current = bind.execute(sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'llm_calls'")).scalar() or ""
    if (("'summary'" in current) == ("summary" in values)):
        return  # already in the wanted shape (a retried upgrade / downgrade)
    before = bind.execute(sa.text("SELECT COUNT(*) FROM llm_calls")).scalar_one()
    seen_post_fk = sa.Column("seen_post_id", sa.Integer(), sa.ForeignKey("seen_posts.id", ondelete="SET NULL"))
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
        try:
            with op.batch_alter_table("llm_calls", recreate="always", reflect_args=[seen_post_fk]) as batch:
                batch.drop_constraint(op.f("ck_llm_calls_feature"), type_="check")
                batch.create_check_constraint("feature", sa.text(_check_sql(values)))
            bad = bind.execute(sa.text("PRAGMA foreign_key_check(llm_calls)")).fetchall()
            if bad:
                raise RuntimeError(f"foreign_key_check found {len(bad)} violation(s) after rebuilding llm_calls")
        finally:
            op.execute("PRAGMA foreign_keys=ON")
    after = bind.execute(sa.text("SELECT COUNT(*) FROM llm_calls")).scalar_one()
    if before != after:
        raise RuntimeError(f"llm_calls rebuild changed the row count: {before} -> {after}")


# ---------------------------------------------------------------------------- tables
def _create_tables() -> None:
    have = _tables()
    if "prospect_docs" not in have:
        op.create_table(
            "prospect_docs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("prospect_id", sa.Integer(), nullable=False),
            sa.Column("doc_type", sa.String(10), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("origin", sa.String(10), nullable=False),
            sa.Column("status", sa.String(10), nullable=False, server_default="done"),
            sa.Column("error", sa.String(500), nullable=True),
            sa.Column("body", sa.Text(), nullable=True),
            sa.Column("data_json", sa.Text(), nullable=True),
            sa.Column("sources_json", sa.Text(), nullable=True),
            sa.Column("inputs_json", sa.Text(), nullable=True),
            sa.Column("based_on_version", sa.Integer(), nullable=True),
            sa.Column("cost_usd_micros", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("web_search_requests", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("company_brief_id", sa.Integer(), nullable=True),
            sa.Column("author_member_id", sa.Integer(), nullable=True),
            sa.Column("actor", sa.String(80), nullable=False, server_default="system"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_prospect_docs")),
            sa.ForeignKeyConstraint(["prospect_id"], ["prospects.id"], ondelete="CASCADE",
                                    name=op.f("fk_prospect_docs_prospect_id_prospects")),
            sa.ForeignKeyConstraint(["company_brief_id"], ["company_briefs.id"], ondelete="SET NULL",
                                    name=op.f("fk_prospect_docs_company_brief_id_company_briefs")),
            sa.ForeignKeyConstraint(["author_member_id"], ["team_members.id"], ondelete="SET NULL",
                                    name=op.f("fk_prospect_docs_author_member_id_team_members")),
            sa.UniqueConstraint("prospect_id", "doc_type", "version", name="uq_prospect_docs_version"),
            sa.CheckConstraint("doc_type IN ('research', 'summary')", name=op.f("ck_prospect_docs_doc_type")),
            sa.CheckConstraint("origin IN ('ai', 'human')", name=op.f("ck_prospect_docs_origin")),
            sa.CheckConstraint("status IN ('done', 'partial', 'failed')", name=op.f("ck_prospect_docs_status")),
        )
        op.create_index("ix_prospect_docs_prospect_type", "prospect_docs", ["prospect_id", "doc_type"])
    if "prospect_notes" not in have:
        op.create_table(
            "prospect_notes",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("prospect_id", sa.Integer(), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("author_member_id", sa.Integer(), nullable=True),
            sa.Column("author", sa.String(80), nullable=False, server_default="team"),
            sa.Column("source", sa.String(10), nullable=False, server_default="app"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_prospect_notes")),
            sa.ForeignKeyConstraint(["prospect_id"], ["prospects.id"], ondelete="CASCADE",
                                    name=op.f("fk_prospect_notes_prospect_id_prospects")),
            sa.ForeignKeyConstraint(["author_member_id"], ["team_members.id"], ondelete="SET NULL",
                                    name=op.f("fk_prospect_notes_author_member_id_team_members")),
            sa.CheckConstraint("source IN ('app', 'migrated')", name=op.f("ck_prospect_notes_source")),
        )
        op.create_index("ix_prospect_notes_prospect_created", "prospect_notes", ["prospect_id", "created_at"])
    if "worker_tasks" not in have:
        op.create_table(
            "worker_tasks",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("status", sa.String(10), nullable=False, server_default="queued"),
            sa.Column("prospect_id", sa.Integer(), nullable=True),
            sa.Column("company_id", sa.Integer(), nullable=True),
            sa.Column("target_url", sa.String(1000), nullable=False),
            sa.Column("linkedin_account_id", sa.Integer(), nullable=True),
            sa.Column("requested_by_member_id", sa.Integer(), nullable=True),
            sa.Column("worker_name", sa.String(80), nullable=True),
            sa.Column("outcome", sa.String(20), nullable=True),
            sa.Column("error", sa.String(500), nullable=True),
            sa.Column("page_loads", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("result_title", sa.String(300), nullable=True),
            sa.Column("result_url", sa.String(1000), nullable=True),
            sa.Column("result_text", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_tasks")),
            sa.ForeignKeyConstraint(["prospect_id"], ["prospects.id"], ondelete="SET NULL",
                                    name=op.f("fk_worker_tasks_prospect_id_prospects")),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="SET NULL",
                                    name=op.f("fk_worker_tasks_company_id_companies")),
            sa.ForeignKeyConstraint(["linkedin_account_id"], ["linkedin_accounts.id"], ondelete="SET NULL",
                                    name=op.f("fk_worker_tasks_linkedin_account_id_linkedin_accounts")),
            sa.ForeignKeyConstraint(["requested_by_member_id"], ["team_members.id"], ondelete="SET NULL",
                                    name=op.f("fk_worker_tasks_requested_by_member_id_team_members")),
            sa.CheckConstraint("kind IN ('company_page')", name=op.f("ck_worker_tasks_kind")),
            sa.CheckConstraint(
                "status IN ('queued', 'claimed', 'done', 'failed', 'cancelled')", name=op.f("ck_worker_tasks_status")
            ),
        )
        op.create_index("ix_worker_tasks_status_created", "worker_tasks", ["status", "created_at"])
        op.create_index("ix_worker_tasks_prospect", "worker_tasks", ["prospect_id"])


# ---------------------------------------------------------------------------- backfills
def _backfill_briefs() -> None:
    bind = op.get_bind()
    done = {r[0] for r in bind.execute(sa.text("SELECT company_brief_id FROM prospect_docs WHERE company_brief_id IS NOT NULL"))}
    rows = bind.execute(
        sa.text(
            "SELECT b.id, b.prospect_id, b.brief_json, b.sources_json, b.cost_usd_micros, b.created_at "
            "FROM company_briefs b JOIN prospects p ON p.id = b.prospect_id ORDER BY b.created_at, b.id"
        )
    ).all()
    docs = sa.table(
        "prospect_docs",
        *(sa.column(n) for n in ("prospect_id", "doc_type", "version", "origin", "status", "error", "data_json",
                                 "sources_json", "inputs_json", "cost_usd_micros", "company_brief_id", "actor")),
        sa.column("created_at"),
    )
    for bid, pid, brief_json, sources_json, cost, created in rows:
        if bid in done:
            continue
        try:
            data = json.loads(brief_json or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        status = data.pop("research_status", None) or "done"
        error = data.pop("research_error", None)
        try:
            urls = json.loads(sources_json or "[]")
        except ValueError:
            urls = []
        urls = [u for u in (urls if isinstance(urls, list) else []) if isinstance(u, str)] or [
            u for u in data.get("sources") or [] if isinstance(u, str)
        ]
        sources = [{"kind": "website", "label": "Company website", "url": u[:1000]} for u in urls]
        latest = bind.execute(
            sa.text("SELECT MAX(version) FROM prospect_docs WHERE prospect_id = :p AND doc_type = 'research'"), {"p": pid}
        ).scalar()
        bind.execute(
            docs.insert(),
            [{
                "prospect_id": pid, "doc_type": "research", "version": int(latest or 0) + 1, "origin": "ai",
                "status": status if status in ("done", "partial", "failed") else "done",
                "error": str(error)[:500] if error else None, "data_json": json.dumps(data, ensure_ascii=False),
                "sources_json": json.dumps(sources, ensure_ascii=False),
                "inputs_json": json.dumps({"website": True, "migrated_from": "company_briefs"}),
                "cost_usd_micros": int(cost or 0), "company_brief_id": bid, "actor": "system",
                "created_at": created if created is not None else _stamp(),
            }],
        )


def _backfill_notes() -> None:
    bind = op.get_bind()
    have = {r[0] for r in bind.execute(sa.text("SELECT DISTINCT prospect_id FROM prospect_notes WHERE source = 'migrated'"))}
    notes = sa.table(
        "prospect_notes",
        sa.column("prospect_id"), sa.column("body"), sa.column("author"), sa.column("source"),
        sa.column("created_at"), sa.column("updated_at"),
    )
    rows = bind.execute(sa.text("SELECT id, notes, updated_at, created_at FROM prospects WHERE notes IS NOT NULL ORDER BY id")).all()
    batch = []
    for pid, text, updated, created in rows:
        if pid in have or not (text or "").strip():
            continue
        stamp = updated or created or _stamp()
        batch.append({"prospect_id": pid, "body": text.strip(), "author": "team", "source": "migrated",
                      "created_at": stamp, "updated_at": stamp})
    if batch:
        bind.execute(notes.insert(), batch)


def upgrade() -> None:
    _create_tables()
    if "web_search_requests" not in _columns("llm_calls"):
        op.add_column("llm_calls", sa.Column("web_search_requests", sa.Integer(), nullable=False, server_default="0"))
    _set_features(FEATURES_NEW)
    _backfill_briefs()
    _backfill_notes()


def downgrade() -> None:
    op.execute("UPDATE llm_calls SET feature = 'research' WHERE feature = 'summary'")
    _set_features(FEATURES_OLD)
    if "web_search_requests" in _columns("llm_calls"):
        op.drop_column("llm_calls", "web_search_requests")
    tables = _tables()
    for table in ("worker_tasks", "prospect_notes", "prospect_docs"):
        if table in tables:
            op.drop_table(table)
