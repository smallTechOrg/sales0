"""Migrations on REALISTIC data (S6).

* the user's real database, when a read-only copy is available (skipped otherwise): copy it again into a tmp
  dir, ``alembic upgrade head``, every pre-existing table keeps its rows (only the documented Phase 1.4 backfill,
  AI discards moving into ``seen_posts``, may change counts), ``PRAGMA foreign_key_check`` is clean, ONE head;
* synthetic populated databases at the p4 and p5 schema (>= 200 enquiries, accepted ones sharing an author, outreach
  messages with events and replies) proving the ``outreach_messages`` rebuilds (p5 ``follow_up_of_id`` and p6 FKs)
  never cascade-delete ``outreach_events`` / ``outreach_replies``.

The real database is NEVER opened in place: only a copy (with its -wal/-shm) is upgraded.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text

from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, current_revision, head_revision
from tests.integration.db._drift import drift
from tests.support.crm_seed import seed_realistic_db

P4 = "p4_ops_c3d8e1f2"
P5 = "p5_replies_c3d81f20"
# Where the build scratchpad keeps the read-only copy (override with SALES0_REALDB_COPY).
DEFAULT_REAL_COPY = Path(
    "/private/tmp/claude-501/-Users-sai-Workspace-Code-sales0/b074bc78-7197-4abe-843f-5eea90d4cf2c/scratchpad/realdb/sales0.db"
)
REAL_COPY = Path(os.environ.get("SALES0_REALDB_COPY", DEFAULT_REAL_COPY))
# The Phase 1.4 migration moves AI discards into seen_posts and may delete nothing else.
BACKFILL_TABLES = {"seen_posts"}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _table_counts(engine: sa.Engine) -> dict[str, int]:
    insp = sa.inspect(engine)
    with engine.connect() as c:
        return {t: int(c.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar_one()) for t in sorted(insp.get_table_names())}


def _assert_clean_head(engine: sa.Engine) -> None:
    assert current_revision(engine) == head_revision()
    with engine.connect() as c:
        assert c.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        assert c.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    assert drift(engine) == []


def test_there_is_exactly_one_alembic_head() -> None:
    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1 and script.get_heads() == [head_revision()]


# ------------------------------------------------------------------------------- the real database (copy)
@pytest.mark.skipif(not REAL_COPY.exists(), reason="no read-only copy of the real database available")
def test_real_database_copy_upgrades_to_head_without_losing_a_row(tmp_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(REAL_COPY) + suffix)
        if src.exists():
            shutil.copy2(src, tmp_path / f"sales0.db{suffix}")
    work = tmp_path / "sales0.db"
    url = _url(work)

    engine = build_engine(url)
    try:
        before = _table_counts(engine)
        before_rev = current_revision(engine)
    finally:
        engine.dispose()
    assert before, "the copy has no tables"

    command.upgrade(alembic_config(url), "head")

    engine = build_engine(url)
    try:
        after = _table_counts(engine)
        _assert_clean_head(engine)
    finally:
        engine.dispose()

    assert before_rev != head_revision() or before == after  # an already-migrated copy is a no-op
    for table, n in before.items():
        assert table in after, f"table {table} vanished"
        if table in BACKFILL_TABLES:
            assert after[table] >= n, f"{table}: {n} -> {after[table]} (the backfill only adds)"
        else:
            assert after[table] == n, f"{table}: {n} rows before, {after[table]} after"
    # The source copy itself was not touched.
    assert REAL_COPY.exists()


# ------------------------------------------------------------------------------- synthetic populated databases
@pytest.fixture
def p4_db(tmp_path: Path):  # type: ignore[no-untyped-def]
    url = _url(tmp_path / "p4.db")
    cfg = alembic_config(url)
    command.upgrade(cfg, P4)
    engine = build_engine(url)
    seed = seed_realistic_db(engine, with_replies=False)
    yield url, cfg, engine, seed
    engine.dispose()


def test_p4_database_with_outreach_events_survives_p5_and_p6(p4_db) -> None:  # type: ignore[no-untyped-def]
    """p5 adds follow_up_of_id with a plain batch rebuild of outreach_messages under foreign_keys=ON: the CASCADE children must survive."""
    url, cfg, engine, seed = p4_db
    assert seed.enquiries >= 200 and seed.events == seed.messages * 4
    before = _table_counts(engine)
    assert before["outreach_events"] == seed.events

    command.upgrade(cfg, P5)
    mid = _table_counts(engine)
    assert mid["outreach_messages"] == seed.messages
    assert mid["outreach_events"] == seed.events, "p5's outreach_messages rebuild cascade-deleted outreach_events"

    command.upgrade(cfg, "head")
    after = _table_counts(engine)
    for table, n in before.items():
        assert after[table] == n, f"{table}: {n} -> {after[table]}"
    _assert_clean_head(engine)
    # 60 accepted enquiries from 5 authors + 25 profile-less ones: one prospect per author, plus one per profile-less accepted enquiry
    with engine.connect() as c:
        accepted_with_profile = c.execute(text("SELECT COUNT(DISTINCT author_profile_id) FROM enquiries WHERE review_status='accepted' AND author_profile_id IS NOT NULL")).scalar_one()
        prospects = c.execute(text("SELECT COUNT(*) FROM prospects")).scalar_one()
        linked = c.execute(text("SELECT COUNT(*) FROM enquiries WHERE prospect_id IS NOT NULL")).scalar_one()
    assert accepted_with_profile == 5 and prospects >= 5 and linked == 60


def test_p5_database_with_replies_survives_p6_and_a_round_trip(tmp_path: Path) -> None:
    url = _url(tmp_path / "p5.db")
    cfg = alembic_config(url)
    command.upgrade(cfg, P5)
    engine = build_engine(url)
    try:
        seed = seed_realistic_db(engine, with_replies=True)
        assert seed.replies >= 8 and seed.events > 0
        before = _table_counts(engine)

        command.upgrade(cfg, "head")
        after = _table_counts(engine)
        for table, n in before.items():
            assert after[table] == n, f"{table}: {n} -> {after[table]}"
        _assert_clean_head(engine)

        # the replies still hang off their messages
        with engine.connect() as c:
            orphans = c.execute(text("SELECT COUNT(*) FROM outreach_replies r LEFT JOIN outreach_messages m ON m.id = r.message_id WHERE m.id IS NULL")).scalar_one()
        assert orphans == 0

        # downgrade / upgrade round trip loses nothing either
        command.downgrade(cfg, "-1")
        assert _table_counts(engine)["outreach_events"] == seed.events
        assert _table_counts(engine)["outreach_replies"] == seed.replies
        command.upgrade(cfg, "head")
        again = _table_counts(engine)
        for table, n in before.items():
            assert again[table] == n, f"{table}: {n} -> {again[table]} after the round trip"
        _assert_clean_head(engine)
    finally:
        engine.dispose()
