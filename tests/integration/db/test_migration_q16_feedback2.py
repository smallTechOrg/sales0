"""Migration q16_feedback2_a41c7e92: follow_ups.planned_channel, outreach_messages.follow_up_id, enquiries.category /
ai_category, prospects.priority (+ source) with a backfill from the linked enquiries; retry-safe; downgrade; no drift.
Runs on PostgreSQL too (``SALES0_TEST_POSTGRES_URL``) through the shared ``db_url`` fixture when set."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, head_revision
from tests.integration.db._drift import drift

PARENT = "q15_research_7d3a9c21"
REV = "q16_feedback2_a41c7e92"
NOW = "2026-10-01 00:00:00"


def _seed(url: str) -> None:
    engine = build_engine(url)
    with engine.begin() as c:
        for pid in (1, 2, 3):
            c.execute(text("INSERT INTO prospects (id, name, created_at, updated_at) VALUES (:i, :n, :d, :d)"),
                      {"i": pid, "n": f"P{pid}", "d": NOW})
        # prospect 1: low + high + medium -> high; prospect 2: only a medium; prospect 3: no enquiry
        rows = [(1, "low", 1), (2, "high", 1), (3, "medium", 1), (4, "medium", 2), (5, None, 3)]
        for eid, prio, pid in rows:
            c.execute(text(
                "INSERT INTO enquiries (id, content_hash, raw_text, source, priority, prospect_id, review_status, triage_status, date_added, origin) "
                "VALUES (:i, :h, 'x', 'linkedin_post', :p, :pid, 'accepted', 'done', :d, 'discovery')"
            ), {"i": eid, "h": f"{eid:064d}", "p": prio, "pid": pid, "d": NOW})
        c.execute(text("INSERT INTO follow_ups (id, prospect_id, kind, due_on, auto, created_at, updated_at) "
                       "VALUES (1, 1, 'follow_up_1', '2026-10-05', 1, :d, :d)"), {"d": NOW})
    engine.dispose()


def test_upgrade_backfills_is_retry_safe_and_downgrades(db_url: str, tmp_path: Path) -> None:
    sqlite = db_url.startswith("sqlite")
    url = f"sqlite:///{tmp_path / 'q16.db'}" if sqlite else db_url
    cfg = alembic_config(url)
    if sqlite:
        command.upgrade(cfg, PARENT)
    else:
        command.downgrade(cfg, PARENT)
    _seed(url)
    command.upgrade(cfg, REV)
    command.upgrade(cfg, REV)  # re-runnable
    engine = build_engine(url)
    try:
        with engine.connect() as c:
            got = dict(c.execute(text("SELECT id, priority FROM prospects")).all())
            assert got == {1: "high", 2: "medium", 3: None}
            assert {r[0] for r in c.execute(text("SELECT priority_source FROM prospects")).all()} == {"derived"}
            assert c.execute(text("SELECT planned_channel FROM follow_ups")).scalar() == "email"
            assert {r[0] for r in c.execute(text("SELECT category FROM enquiries")).all()} == {"relevant_prospect"}
            assert c.execute(text("SELECT COUNT(*) FROM enquiries WHERE ai_category IS NOT NULL")).scalar() == 0
        assert drift(engine) == []
        command.downgrade(cfg, PARENT)
        insp = inspect(engine)
        assert "priority" not in {x["name"] for x in insp.get_columns("prospects")}
        assert "category" not in {x["name"] for x in insp.get_columns("enquiries")}
        assert "planned_channel" not in {x["name"] for x in insp.get_columns("follow_ups")}
        assert "follow_up_id" not in {x["name"] for x in insp.get_columns("outreach_messages")}
        command.upgrade(cfg, "head")
        assert drift(engine) == []
    finally:
        engine.dispose()


def test_revision_chain() -> None:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    rev = script.get_revision(REV)
    assert rev is not None and rev.down_revision == PARENT
    assert REV in {r.revision for r in script.iterate_revisions(head_revision(), "base")}
