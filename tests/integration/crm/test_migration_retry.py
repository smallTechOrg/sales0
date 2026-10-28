"""M4: the p6 migration is retry-safe on SQLite (DDL is not transactional): a mid-backfill failure, then a rerun."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import text

from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, current_revision, head_revision
from tests.support.crm_seed import seed_realistic_db

P5 = "p5_replies_c3d81f20"


def _counts(engine: sa.Engine) -> dict[str, int]:
    with engine.connect() as c:
        return {t: int(c.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar_one()) for t in sorted(sa.inspect(engine).get_table_names())}


def _p5_db(path: Path):  # type: ignore[no-untyped-def]
    url = f"sqlite:///{path.as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, P5)
    engine = build_engine(url)
    seed_realistic_db(engine, with_replies=True)
    return url, cfg, engine


def test_upgrade_is_retry_safe_after_a_mid_backfill_failure(tmp_path: Path) -> None:
    # reference: a clean single run
    ref_url, ref_cfg, ref_engine = _p5_db(tmp_path / "ref.db")
    command.upgrade(ref_cfg, "head")
    expected = _counts(ref_engine)
    ref_engine.dispose()

    url, cfg, engine = _p5_db(tmp_path / "boom.db")
    try:
        before = _counts(engine)
        with engine.begin() as c:  # fail while linking the 3rd accepted enquiry to its new company
            third = c.execute(text("SELECT id FROM enquiries WHERE review_status='accepted' ORDER BY id LIMIT 1 OFFSET 2")).scalar_one()
            c.execute(text(
                "CREATE TRIGGER boom BEFORE UPDATE OF prospect_id ON enquiries WHEN NEW.prospect_id IS NOT NULL AND NEW.id = %d "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END" % third))
        with pytest.raises(Exception, match="boom"):
            command.upgrade(cfg, "head")
        assert current_revision(engine) == P5
        # the failed run left (some of) its DDL behind, which is exactly what the retry must tolerate
        assert "prospects" in sa.inspect(engine).get_table_names()
        with engine.begin() as c:
            c.execute(text("DROP TRIGGER boom"))

        command.upgrade(cfg, "head")
        assert current_revision(engine) == head_revision()
        after = _counts(engine)
        for table, n in before.items():
            if table == "users":
                continue  # retired by p7 (team members are the users); it was empty
            assert after[table] == n, f"{table}: {n} -> {after[table]}"
        assert after == expected, "a retried upgrade must end in the same state as a clean one"
        with engine.connect() as c:
            assert c.execute(text("PRAGMA foreign_key_check")).fetchall() == []
            assert c.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    finally:
        engine.dispose()
