"""Migration p8_liaccts_b2e5f7a9: linkedin_accounts + scrape_runs.linkedin_account_id + default-account backfill."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text

from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, current_revision, head_revision
from tests.integration.db._drift import assert_inline_fks_cascade, drift

P6 = "p6_crm_5b2e7d14"
P7 = "p7_accounts_a1d4c6e8"
P8 = "p8_liaccts_b2e5f7a9"
N = "2026-10-20 10:00:00.000000"
CHECKED = "2026-10-27 09:30:00.000000"
CONNECTED = "2026-10-26 08:00:00.000000"
LAST_RUN = "2026-10-27 09:00:00.000000"


def _run_sql(status: str, abort: str | None = None) -> tuple[str, dict[str, object]]:
    return (
        "INSERT INTO scrape_runs (trigger, mode, status, started_at, finished_at, abort_reason, keywords_planned, keywords_done, "
        "posts_seen, posts_new, posts_dupe, posts_relevant, posts_skipped_ai, permalinks_found, enquiries_triaged, triage_failed, errors_count) "
        "VALUES ('manual', 'live', :s, :n, :n, :a, 0, 0, 5, 0, 0, 0, 0, 0, 0, 0, 0)",
        {"s": status, "a": abort, "n": N},
    )


@pytest.fixture
def p6_db(tmp_path):  # type: ignore[no-untyped-def]
    """A DB at p6 with a connected linkedin_session singleton and three scrape_runs (one safety-aborted)."""
    url = f"sqlite:///{(tmp_path / 'p6.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, P6)
    engine = build_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE linkedin_session SET status='connected', last_checked_at=:c, last_connected_at=:k, "
                "last_successful_run_at=:r, last_error=NULL WHERE id = 1"
            ),
            {"c": CHECKED, "k": CONNECTED, "r": LAST_RUN},
        )
        for status, abort in (("succeeded", None), ("aborted", "captcha"), ("succeeded", None)):
            sql, params = _run_sql(status, abort)
            c.execute(text(sql), params)
    engine.dispose()
    return {"url": url, "cfg": cfg}


def _rows(url: str, sql: str) -> list:
    engine = build_engine(url)
    try:
        with engine.connect() as c:
            return list(c.execute(text(sql)).mappings().all())
    finally:
        engine.dispose()


def test_backfill_creates_the_default_account_from_the_singleton_and_stamps_runs(p6_db) -> None:  # type: ignore[no-untyped-def]
    command.upgrade(p6_db["cfg"], P8)
    accounts = _rows(p6_db["url"], "SELECT * FROM linkedin_accounts")
    assert len(accounts) == 1
    acct = accounts[0]
    assert acct["label"] == "Default" and acct["backend"] == "folder" and acct["disabled"] == 0
    assert acct["status"] == "connected"
    assert str(acct["last_checked_at"]).startswith("2026-10-27 09:30")
    assert str(acct["last_connected_at"]).startswith("2026-10-26 08:00")
    assert str(acct["last_successful_run_at"]).startswith("2026-10-27 09:00")
    assert acct["profile_dirname"] in {
        "linkedin-chrome-profile", "linkedin-profile", "linkedin-profile-fixture-chrome", "linkedin-profile-fixture-chromium"
    }
    assert acct["session_state"] is None
    stamped = _rows(p6_db["url"], "SELECT linkedin_account_id AS a, COUNT(*) AS n FROM scrape_runs GROUP BY 1")
    assert [(r["a"], r["n"]) for r in stamped] == [(acct["id"], 3)]
    # the legacy singleton is kept, untouched
    assert _rows(p6_db["url"], "SELECT status FROM linkedin_session")[0]["status"] == "connected"


def test_upgrade_is_retry_safe_and_does_not_duplicate_the_default_account(p6_db) -> None:  # type: ignore[no-untyped-def]
    command.upgrade(p6_db["cfg"], P8)
    # Simulate a half-finished earlier attempt: version stamp rolled back, runs unstamped.
    engine = build_engine(p6_db["url"])
    with engine.begin() as c:
        c.execute(text("UPDATE scrape_runs SET linkedin_account_id = NULL"))
        c.execute(text(f"UPDATE alembic_version SET version_num = '{P7}'"))
    engine.dispose()
    command.upgrade(p6_db["cfg"], "head")
    assert len(_rows(p6_db["url"], "SELECT id FROM linkedin_accounts")) == 1
    assert _rows(p6_db["url"], "SELECT COUNT(*) AS n FROM scrape_runs WHERE linkedin_account_id IS NULL")[0]["n"] == 0


def test_downgrade_then_upgrade_round_trips(p6_db) -> None:  # type: ignore[no-untyped-def]
    command.upgrade(p6_db["cfg"], P8)
    command.downgrade(p6_db["cfg"], P7)
    engine = build_engine(p6_db["url"])
    try:
        assert "linkedin_accounts" not in inspect(engine).get_table_names()
        assert current_revision(engine) == P7
    finally:
        engine.dispose()
    command.upgrade(p6_db["cfg"], "head")
    assert len(_rows(p6_db["url"], "SELECT id FROM linkedin_accounts")) == 1
    assert _rows(p6_db["url"], "SELECT COUNT(*) AS n FROM scrape_runs WHERE linkedin_account_id IS NOT NULL")[0]["n"] == 3


def test_a_needs_attention_singleton_keeps_its_status_and_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    url = f"sqlite:///{(tmp_path / 'na.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, P6)
    engine = build_engine(url)
    with engine.begin() as c:
        c.execute(text("UPDATE linkedin_session SET status='needs_attention', last_error='Captcha shown at /x' WHERE id = 1"))
    engine.dispose()
    command.upgrade(cfg, P8)
    row = _rows(url, "SELECT status, last_error FROM linkedin_accounts")[0]
    assert (row["status"], row["last_error"]) == ("needs_attention", "Captcha shown at /x")


def test_a_fresh_database_gets_a_not_connected_default_account_and_matches_the_models(db_url: str) -> None:
    engine = build_engine(db_url)
    try:
        assert current_revision(engine) == head_revision()
        assert drift(engine) == []
        assert_inline_fks_cascade(engine)
    finally:
        engine.dispose()
    rows = _rows(db_url, "SELECT label, status, backend FROM linkedin_accounts")
    assert [(r["label"], r["status"], r["backend"]) for r in rows] == [("Default", "not_connected", "folder")]


def test_the_check_constraints_reject_bad_status_and_backend(db_url: str) -> None:
    from sqlalchemy.exc import IntegrityError

    engine = build_engine(db_url)
    try:
        for column, bad in (("status", "bogus"), ("backend", "cloud")):
            with pytest.raises(IntegrityError), engine.begin() as c:
                c.execute(
                    text(f"INSERT INTO linkedin_accounts (label, {column}, created_at, updated_at) VALUES ('x', :v, :n, :n)"),
                    {"v": bad, "n": N},
                )
    finally:
        engine.dispose()


def test_a_copy_of_the_live_database_upgrades_to_head_with_a_default_account(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Only when SALES0_LIVE_DB_COPY names a SQLite file. It is COPIED first; the original is never opened for writing."""
    import os

    source = os.environ.get("SALES0_LIVE_DB_COPY")
    if not source:
        pytest.skip("SALES0_LIVE_DB_COPY is not set (opt-in: point it at a copy of the live SQLite file)")
    src = Path(source)
    assert src.is_file(), src
    dst = tmp_path / "live-copy.db"
    shutil.copyfile(src, dst)
    for suffix in ("-wal", "-shm"):
        extra = Path(str(src) + suffix)
        if extra.is_file():
            shutil.copyfile(extra, Path(str(dst) + suffix))
    url = f"sqlite:///{dst.as_posix()}"
    command.upgrade(alembic_config(url), "head")
    accounts = _rows(url, "SELECT id, backend FROM linkedin_accounts ORDER BY id")
    assert accounts and accounts[0]["backend"] == "folder"
    unstamped = _rows(url, "SELECT COUNT(*) AS n FROM scrape_runs WHERE linkedin_account_id IS NULL")[0]["n"]
    assert unstamped == 0
