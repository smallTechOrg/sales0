"""Migration p11_rawcapture_a4d9c7e2: five nullable columns on seen_posts; no data touched; downgrade removes them."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from sqlalchemy import inspect, text

from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, head_revision

PARENT = "p10_worker_e6c2b9d4"
REV = "p11_rawcapture_a4d9c7e2"
NEW = {"posted_date", "truncated", "link_kind", "search_url", "raw_card"}


def cols(url: str) -> set[str]:
    engine = build_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("seen_posts")}
    finally:
        engine.dispose()


def test_adds_and_removes_the_columns_and_keeps_rows(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = alembic_config(url)
    command.upgrade(cfg, PARENT)
    assert not NEW & cols(url)
    engine = build_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO seen_posts (fingerprint, content_hash, keyword_text, first_seen_at, last_seen_at) VALUES ('f', 'h', 'k', '2026-10-01 00:00:00', '2026-10-01 00:00:00')"))
    engine.dispose()
    command.upgrade(cfg, REV)
    assert NEW <= cols(url)
    engine = build_engine(url)
    with engine.connect() as c:
        row = c.execute(text("SELECT fingerprint, posted_date, truncated, link_kind, search_url, raw_card FROM seen_posts")).one()
    engine.dispose()
    assert tuple(row) == ("f", None, None, None, None, None)
    command.upgrade(cfg, REV)  # re-runnable
    command.downgrade(cfg, PARENT)
    assert not NEW & cols(url)


def test_it_is_on_the_single_linear_chain() -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    assert REV in {rev.revision for rev in script.walk_revisions("base", head_revision())}
