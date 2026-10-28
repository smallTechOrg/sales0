"""p7 (per-user accounts): team members ARE the users.

* synthetic database at p6 WITH legacy ``users`` rows and ``actor_user_id`` references on all five tables:
  upgrade loses no row, remaps the attribution to (inactive) legacy team members, repoints every FK at
  ``team_members`` keeping each ``ON DELETE`` rule, keeps the children of ``enquiries``, adds the account
  columns/tables, drops ``users``; downgrade/upgrade round-trips; a failure mid-remap is retried cleanly;
* a copy of the live database (skipped unless ``SALES0_LIVE_DB_COPY`` points to a file at p5): upgrade to HEAD
  (also slice B's p8), counts preserved, FKs clean, no drift. Only counts are asserted/printed - the data is
  personal and is never written into the repository.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import text

from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, current_revision, head_revision
from tests.integration.db._drift import drift

P6 = "p6_crm_5b2e7d14"
P7 = "p7_accounts_a1d4c6e8"
ACTOR_TABLES = ("enquiries", "keywords", "scrape_runs", "status_history", "triage_decisions")
NOW = "2026-10-01 10:00:00"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _counts(engine: sa.Engine) -> dict[str, int]:
    with engine.connect() as c:
        return {t: int(c.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar_one()) for t in sorted(sa.inspect(engine).get_table_names())}


def _fk_rules(engine: sa.Engine, table: str) -> dict[str, tuple[str, str]]:
    """column -> (referenced table, on-delete rule), from the real schema (PRAGMA keeps inline rules)."""
    with engine.connect() as c:
        return {r[3]: (r[2], r[6]) for r in c.execute(text(f"PRAGMA foreign_key_list({table})"))}


def _seed_p6(engine: sa.Engine) -> dict[str, int]:
    """Legacy users 1,2,3 (2 shares the email of an existing team member), actor refs incl. one dangling id."""
    with engine.begin() as c:
        c.execute(text("PRAGMA foreign_keys=OFF"))
        c.execute(text("INSERT INTO team_members (id, name, email, role, active, created_at) VALUES (1,'Ada Admin','ada@old.example','admin',1,:n),(2,'Bo Member','bo@old.example','member',1,:n)"), {"n": NOW})
        c.execute(text("INSERT INTO users (id, display_name, email, created_at) VALUES (1,'Legacy One',NULL,:n),(2,'Bo Again','bo@old.example',:n),(3,'Legacy Three','three@old.example',:n)"), {"n": NOW})
        for i in range(1, 6):
            actor = {1: 1, 2: 2, 3: 3, 4: None, 5: 99}[i]  # 99 = dangling (no such user)
            c.execute(text("INSERT INTO enquiries (content_hash, raw_text, date_added, actor_user_id) VALUES (:h,'raw',:n,:a)"), {"h": f"h{i}", "n": NOW, "a": actor})
            c.execute(text("INSERT INTO keywords (text, text_key, created_at, actor_user_id) VALUES (:t,:t,:n,:a)"), {"t": f"kw{i}", "n": NOW, "a": actor})
            c.execute(text("INSERT INTO scrape_runs (mode, started_at, actor_user_id) VALUES ('fixture',:n,:a)"), {"n": NOW, "a": actor})
            c.execute(text("INSERT INTO status_history (entity_type, entity_id, field, changed_at, actor_user_id) VALUES ('enquiry',:i,'review_status',:n,:a)"), {"i": i, "n": NOW, "a": actor})
            c.execute(text("INSERT INTO triage_decisions (enquiry_id, decision, is_override, decided_at, actor_user_id) VALUES (:i,'accept',0,:n,:a)"), {"i": i, "n": NOW, "a": actor})
            c.execute(text("INSERT INTO enquiry_sources (enquiry_id, source_ref, first_seen_at) VALUES (:i,:r,:n)"), {"i": i, "r": f"r{i}", "n": NOW})
        c.execute(text("PRAGMA foreign_keys=ON"))
    return _counts(engine)


@pytest.fixture
def p6_db(tmp_path: Path):  # type: ignore[no-untyped-def]
    url = _url(tmp_path / "p6.db")
    cfg = alembic_config(url)
    command.upgrade(cfg, P6)
    engine = build_engine(url)
    seeded = _seed_p6(engine)
    yield {"url": url, "cfg": cfg, "engine": engine, "before": seeded}
    engine.dispose()


def _actors(engine: sa.Engine, table: str) -> list[int | None]:
    with engine.connect() as c:
        return [r[0] for r in c.execute(text(f"SELECT actor_user_id FROM {table} ORDER BY id"))]


def test_upgrade_remaps_attribution_loses_no_row_and_repoints_every_fk(p6_db) -> None:  # type: ignore[no-untyped-def]
    engine, cfg, before = p6_db["engine"], p6_db["cfg"], p6_db["before"]
    inline_before = {t: _fk_rules(engine, t) for t in ACTOR_TABLES}
    command.upgrade(cfg, P7)
    after = _counts(engine)
    assert "users" not in after
    for table, n in before.items():
        if table != "users":
            assert after[table] == n or table == "team_members", f"{table}: {n} -> {after[table]}"
    assert {"member_sessions", "password_tokens"} <= set(after)

    with engine.connect() as c:
        members = {r.email: r for r in c.execute(text("SELECT id, email, active, role FROM team_members"))}
        assert c.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        cols = {r[1] for r in c.execute(text("PRAGMA table_info(team_members)"))}
    assert {"password_hash", "password_set_at", "must_reset", "last_login_at"} <= cols
    # user 1 had no email: a placeholder legacy member; user 2 matched Bo by email; user 3 is new. Legacy ones are inactive.
    legacy1, legacy3 = members["legacy-user-1@users.invalid"], members["three@old.example"]
    assert not legacy1.active and not legacy3.active and legacy1.id > 2 and legacy3.id > 2
    assert len(members) == 4  # Ada, Bo + 2 legacy (user 2 maps to Bo)
    expected = [legacy1.id, members["bo@old.example"].id, legacy3.id, None, None]  # 99 was dangling -> NULL
    for table in ACTOR_TABLES:
        assert _actors(engine, table) == expected, table

    for table in ACTOR_TABLES:
        rules = _fk_rules(engine, table)
        assert rules["actor_user_id"] == ("team_members", "SET NULL"), table
        # every OTHER foreign key keeps its target and its ON DELETE rule (reflection drops inline rules: restated)
        for col, rule in inline_before[table].items():
            if col != "actor_user_id":
                assert rules[col] == rule, f"{table}.{col}: {rule} -> {rules[col]}"
    with engine.connect() as c:  # the SET NULL rule really works on the new FK
        c.execute(text("PRAGMA foreign_keys=ON"))
        c.execute(text(f"DELETE FROM team_members WHERE id = {legacy1.id}"))
        c.commit()
    assert _actors(engine, "enquiries")[0] is None
    command.upgrade(cfg, "head")
    with engine.connect() as c:
        assert c.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    assert drift(engine) == [] and current_revision(engine) == head_revision()


def test_children_of_enquiries_survive_and_indexes_are_kept(p6_db) -> None:  # type: ignore[no-untyped-def]
    engine, cfg = p6_db["engine"], p6_db["cfg"]
    idx_before = {t: {r[1] for r in engine.connect().execute(text(f"PRAGMA index_list({t})"))} for t in ACTOR_TABLES}
    command.upgrade(cfg, P7)
    c = _counts(engine)
    assert c["enquiry_sources"] == 5 and c["triage_decisions"] == 5 and c["enquiries"] == 5
    for t in ACTOR_TABLES:
        now = {r[1] for r in engine.connect().execute(text(f"PRAGMA index_list({t})"))}
        assert idx_before[t] <= now, f"{t}: lost {idx_before[t] - now}"


def test_downgrade_then_upgrade_round_trips_with_data(p6_db) -> None:  # type: ignore[no-untyped-def]
    engine, cfg = p6_db["engine"], p6_db["cfg"]
    command.upgrade(cfg, P7)
    mid = _counts(engine)
    command.downgrade(cfg, P6)
    back = _counts(engine)
    assert current_revision(engine) == P6 and "member_sessions" not in back and "password_tokens" not in back and "users" in back
    for t in ACTOR_TABLES:
        assert back[t] == mid[t]
        assert _fk_rules(engine, t)["actor_user_id"] == ("users", "SET NULL")
    with engine.connect() as c:
        assert c.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        assert "password_hash" not in {r[1] for r in c.execute(text("PRAGMA table_info(team_members)"))}
    command.upgrade(cfg, P7)
    assert _counts(engine)["team_members"] == mid["team_members"]


def test_upgrade_is_retry_safe_after_a_failure_mid_remap(p6_db) -> None:  # type: ignore[no-untyped-def]
    engine, cfg = p6_db["engine"], p6_db["cfg"]
    with engine.begin() as c:
        c.execute(text("CREATE TRIGGER boom BEFORE UPDATE OF actor_user_id ON status_history BEGIN SELECT RAISE(ABORT, 'boom'); END"))
    with pytest.raises(Exception, match="boom"):
        command.upgrade(cfg, P7)
    assert current_revision(engine) == P6
    with engine.begin() as c:
        c.execute(text("DROP TRIGGER boom"))
    command.upgrade(cfg, P7)
    assert current_revision(engine) == P7
    members = {r[0]: r[1] for r in engine.connect().execute(text("SELECT email, id FROM team_members"))}
    assert len(members) == 4  # no duplicate legacy members from the first attempt
    expected = [members["legacy-user-1@users.invalid"], members["bo@old.example"], members["three@old.example"], None, None]
    for table in ACTOR_TABLES:
        assert _actors(engine, table) == expected, table  # not remapped twice
    with engine.connect() as c:
        assert c.execute(text("PRAGMA foreign_key_check")).fetchall() == []


def test_empty_database_upgrades_with_only_the_new_objects(tmp_path: Path) -> None:
    url = _url(tmp_path / "empty.db")
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    engine = build_engine(url)
    try:
        assert "users" not in _counts(engine)
        assert drift(engine) == []
    finally:
        engine.dispose()


# ------------------------------------------------------------------------------- the live database (copy)
LIVE_COPY = os.environ.get("SALES0_LIVE_DB_COPY")


@pytest.mark.skipif(not LIVE_COPY or not Path(LIVE_COPY).exists(), reason="set SALES0_LIVE_DB_COPY to a copy of the live DB (at p5)")
def test_live_database_copy_upgrades_to_head_preserving_counts(tmp_path: Path) -> None:
    src = Path(LIVE_COPY)  # type: ignore[arg-type]
    work = tmp_path / "live.db"
    # copy via SQLite's backup API so a -wal beside the original is included; the original is only read
    import sqlite3

    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(work) as d:
        s.backup(d)
    url = _url(work)
    engine = build_engine(url)
    try:
        before = _counts(engine)
        assert current_revision(engine) is not None
        command.upgrade(alembic_config(url), "head")
        after = _counts(engine)
        with engine.connect() as c:
            assert c.execute(text("PRAGMA foreign_key_check")).fetchall() == []
            assert c.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
            actors = {t: c.execute(text(f"SELECT COUNT(*) FROM {t} WHERE actor_user_id IS NOT NULL")).scalar_one() for t in ACTOR_TABLES}
        assert current_revision(engine) == head_revision() and drift(engine) == []
        grown = {"prospects", "companies", "contacts", "timeline_events", "seen_posts", "linkedin_accounts", "clients", "leads"}
        for table, n in before.items():
            if table == "users":
                assert "users" not in after and n == 0, "the retired users table must have been empty (counts only, no rows are shown)"
            elif table not in grown:
                assert after[table] >= n, f"{table}: {n} -> {after[table]}"
        assert after["team_members"] >= before["team_members"]
        print(f"live-copy migration ok: tables={len(after)} members={after['team_members']} actor_refs={actors}")  # counts only
    finally:
        engine.dispose()
