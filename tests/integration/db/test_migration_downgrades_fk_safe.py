"""Downgrades of 0003/0004 must not cascade-delete child rows (SQLite batch rebuild with foreign_keys=ON).

Each test upgrades to the revision under test, seeds child rows that reference the tables the downgrade
rebuilds, runs ``downgrade -1`` from that revision (not head) and asserts the children survive and
``foreign_key_check`` is clean.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command

from sales0.db.migrate_check import alembic_config

R0003 = "0003_phase1_4_seen_posts"
R0004 = "0004_opportunity_type"
NOW = "2026-10-28 00:00:00"


def _insert(c: sqlite3.Connection, table: str, **given: object) -> int:
    """Insert one row, filling every other NOT NULL column that has no default with a type-based value."""
    values = dict(given)
    for _cid, name, ctype, notnull, default, pk in c.execute(f"PRAGMA table_info({table})").fetchall():
        if name in values or pk or not notnull or default is not None:
            continue
        t = (ctype or "").upper()
        if "INT" in t or "BOOL" in t:
            values[name] = 0
        elif "DATE" in t or "TIME" in t:
            values[name] = NOW
        elif "JSON" in t:
            values[name] = "{}"
        else:
            values[name] = f"{table}-{name}"
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    return int(c.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values())).lastrowid)  # type: ignore[arg-type]


def _seed(path: Path) -> None:
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys=ON")
    kw = _insert(c, "keywords", text="kw", text_key="kw", active=1, created_at=NOW)
    run = _insert(c, "scrape_runs", trigger="manual", mode="fixture", status="succeeded", started_at=NOW)
    _insert(c, "scrape_run_keywords", run_id=run, keyword_id=kw, keyword_text="kw", status="done")
    enq = _insert(
        c, "enquiries", source="linkedin_post", date_added=NOW, review_status="pending_review", triage_status="done",
        content_hash="h" * 64, raw_text="t",
    )
    _insert(c, "enquiry_sources", enquiry_id=enq, first_seen_run_id=run)
    _insert(c, "enquiry_keyword_hits", enquiry_id=enq, keyword_id=kw, run_id=run)
    _insert(c, "triage_decisions", enquiry_id=enq, decision="accept")
    llm = {"feature": "triage", "model": "m", "enquiry_id": enq, "run_id": run, "status": "ok", "created_at": NOW}
    _insert(c, "llm_calls", **llm)
    c.commit()
    c.close()


def _counts(path: Path) -> dict[str, int]:
    c = sqlite3.connect(path)
    try:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: c.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        c.close()


CHILDREN = ("scrape_run_keywords", "enquiry_sources", "enquiry_keyword_hits", "triage_decisions", "llm_calls")


@pytest.mark.parametrize("revision", [R0004, R0003])
def test_downgrade_one_step_preserves_child_rows(tmp_path: Path, revision: str) -> None:
    path = tmp_path / "d.db"
    url = f"sqlite:///{path.as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, revision)
    _seed(path)
    before = _counts(path)
    assert all(before[t] == 1 for t in CHILDREN), before

    # `-1` relative to the current revision == the downgrade of the revision the file defines.
    command.downgrade(cfg, "-1")

    after = _counts(path)
    for table in CHILDREN:
        assert after[table] == before[table], f"{table} lost rows in downgrade of {revision}"
    assert after["enquiries"] == before["enquiries"]
    c = sqlite3.connect(path)
    try:
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        c.close()
