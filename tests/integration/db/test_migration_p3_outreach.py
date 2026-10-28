"""Migration p3_outreach_a7c41e9d on a real SQLite file: upgrade, tables, downgrade."""

from __future__ import annotations

from alembic import command
from sqlalchemy import inspect

from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, current_revision, head_revision

REVISION = "p3_outreach_a7c41e9d"
P3_TABLES = {
    "team_members",
    "gmail_connections",
    "message_templates",
    "decks",
    "outreach_messages",
    "outreach_events",
    "company_briefs",
}


def _tables(url: str) -> set[str]:
    engine = build_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_creates_every_outreach_table_and_is_head(db_url: str) -> None:
    assert head_revision() == REVISION
    engine = build_engine(db_url)
    try:
        assert current_revision(engine) == REVISION
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("outreach_messages")}
        assert {"state", "content_sha256", "send_outcome_unknown", "gmail_thread_id", "brief_json"} <= cols
        # CRM references are plain integers: no FK to tables that do not exist yet.
        fk_cols = {c for fk in insp.get_foreign_keys("outreach_messages") for c in fk["constrained_columns"]}
        assert fk_cols == {"member_id", "approved_by_member_id"}
        assert [fk["referred_table"] for fk in insp.get_foreign_keys("gmail_connections")] == ["team_members"]
    finally:
        engine.dispose()
    assert P3_TABLES <= _tables(db_url)


def test_downgrade_removes_only_the_outreach_tables(db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "0003_sheet_sync_state")
    remaining = _tables(db_url)
    assert remaining & P3_TABLES == set()
    assert {"enquiries", "keywords"} <= remaining
    command.upgrade(cfg, "head")
    assert P3_TABLES <= _tables(db_url)
