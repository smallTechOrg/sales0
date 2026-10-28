"""Migration p9_scrapesafe_d3f8a1c6: scrape_runs.page_loads."""

from __future__ import annotations

from alembic import command
from sqlalchemy import inspect, text

from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, current_revision, head_revision
from tests.integration.db._drift import drift

P8 = "p9_lirekey_7c3e9a15"  # previous head (scrapesafe is chained after lirekey)
P9 = "p9_scrapesafe_d3f8a1c6"


def test_p9_adds_page_loads_with_zero_for_old_runs_is_retry_safe_and_reversible(tmp_path) -> None:  # type: ignore[no-untyped-def]
    url = f"sqlite:///{(tmp_path / 'p9.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, P8)
    engine = build_engine(url)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO scrape_runs (trigger, mode, status, keywords_planned, keywords_done, posts_seen, posts_new, posts_dupe, "
            "posts_relevant, posts_skipped_ai, permalinks_found, enquiries_triaged, triage_failed, errors_count) "
            "VALUES ('manual','live','succeeded',0,0,5,0,0,0,0,0,0,0,0)"
        ))
    command.upgrade(cfg, P9)
    assert current_revision(engine) == P9
    with engine.connect() as c:
        assert c.execute(text("SELECT page_loads FROM scrape_runs")).scalar() == 0
    assert "page_loads" in {col["name"] for col in inspect(engine).get_columns("scrape_runs")}
    # retry-safe: stamp back and upgrade again
    command.stamp(cfg, P8)
    command.upgrade(cfg, P9)
    command.upgrade(cfg, "head")
    assert drift(engine) == []
    assert current_revision(engine) == head_revision()
    command.downgrade(cfg, P8)
    assert "page_loads" not in {col["name"] for col in inspect(engine).get_columns("scrape_runs")}
    engine.dispose()
