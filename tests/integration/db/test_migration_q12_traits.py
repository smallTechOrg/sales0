"""Migration q12_traits_9d2c4a71: characteristics registry (seeded) + entity_characteristics, company matching keys
(backfilled from website / LinkedIn URL), linkedin_profiles.watch_note; re-runnable; downgrade removes it all."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from sqlalchemy import inspect, text

from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, head_revision
from tests.integration.db._drift import drift

PARENT = "p11_rawcapture_a4d9c7e2"
REV = "q12_traits_9d2c4a71"


def _schema(url: str) -> tuple[set[str], set[str], set[str]]:
    engine = build_engine(url)
    try:
        insp = inspect(engine)
        return (set(insp.get_table_names()), {c["name"] for c in insp.get_columns("companies")},
                {c["name"] for c in insp.get_columns("linkedin_profiles")})
    finally:
        engine.dispose()


def test_upgrade_seeds_backfills_and_downgrades(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = alembic_config(url)
    command.upgrade(cfg, PARENT)
    engine = build_engine(url)
    with engine.begin() as c:
        for i, (name, web, li) in enumerate([
            ("Acme Ltd", "https://www.acme.example/about", "https://www.linkedin.com/company/Acme-Ltd/"),
            ("Gmail Person Co", "gmail.com", None),
            ("Bare", None, None),
        ], start=1):
            c.execute(text("INSERT INTO companies (id, name, name_key, website, linkedin_url, created_at, updated_at) "
                           "VALUES (:i, :n, :k, :w, :l, '2026-10-01 00:00:00', '2026-10-01 00:00:00')"),
                      {"i": i, "n": name, "k": name.lower(), "w": web, "l": li})
    engine.dispose()

    command.upgrade(cfg, REV)
    tables, company_cols, profile_cols = _schema(url)
    assert {"characteristics", "entity_characteristics"} <= tables
    assert {"domain_key", "linkedin_key"} <= company_cols and "watch_note" in profile_cols
    engine = build_engine(url)
    with engine.connect() as c:
        seeds = c.execute(text("SELECT name, system_key, active FROM characteristics ORDER BY sort_order")).all()
        keys = dict(c.execute(text("SELECT id, domain_key || '|' || COALESCE(linkedin_key, '') FROM companies")).all())
        nulls = c.execute(text("SELECT domain_key, linkedin_key FROM companies WHERE id IN (2, 3) ORDER BY id")).all()
    assert [(n, s, bool(a)) for n, s, a in seeds] == [
        ("Training provider", "training_provider", True), ("Partner", "partner", True), ("Outreach client", "outreach_client", True)]
    assert keys[1] == "acme.example|company/acme-ltd"
    assert [tuple(r) for r in nulls] == [(None, None), (None, None)]  # gmail.com is a generic host, never a company key
    assert drift(engine) == []
    engine.dispose()

    command.upgrade(cfg, REV)  # re-runnable, no duplicate seeds
    command.downgrade(cfg, PARENT)
    tables, company_cols, profile_cols = _schema(url)
    assert not {"characteristics", "entity_characteristics"} & tables
    assert not {"domain_key", "linkedin_key"} & company_cols and "watch_note" not in profile_cols
    command.upgrade(cfg, REV)
    engine = build_engine(url)
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM characteristics")).scalar_one() == 3
        assert c.execute(text("SELECT COUNT(*) FROM companies")).scalar_one() == 3
    engine.dispose()


def test_it_is_the_single_linear_head() -> None:
    assert head_revision() == REV
