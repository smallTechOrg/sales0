"""Migration q14_criteria_5b8e1f3a: criteria tables seeded, new columns, search links fixed; retry-safe; downgrade.

Synthetic rows only. Head is read via head_revision() (the integrator re-links the chain)."""

from __future__ import annotations

import json
from pathlib import Path

from alembic import command
from sqlalchemy import inspect, text

from sales0.agent.criteria import DEFAULT_EXCLUDE, DEFAULT_INCLUDE
from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, head_revision
from tests.integration.db._drift import drift

PARENT = "q13_followch_3e7a6c58"
REV = "q14_criteria_5b8e1f3a"
LI = "https://www.linkedin.com"
SEARCH = f"{LI}/search/results/content/?keywords=ai%20workshop"
NOW = "2026-10-01 00:00:00"


def _cols(url: str, table: str) -> set[str]:
    engine = build_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def _seed_parent(url: str) -> None:
    engine = build_engine(url)
    with engine.begin() as c:
        c.execute(text(f"INSERT INTO linkedin_profiles (id, slug, kind, profile_url, first_seen_at, last_seen_at) VALUES "
                       f"(1, 'in/asha-fx', 'person', '{LI}/in/asha-fx/', '{NOW}', '{NOW}')"))
        c.execute(text(
            "INSERT INTO seen_posts (id, fingerprint, content_hash, keyword_text, first_seen_at, last_seen_at, raw_card) "
            f"VALUES (1, 'f1', 'c1', 'k', '{NOW}', '{NOW}', :card)"
        ), {"card": json.dumps({"author_profile_url": f"{LI}/in/ravi-fx/?miniProfile=1"})})
        rows = [
            # id, source_url, author_profile_url, author_profile_id, seen_post_id
            (1, SEARCH, f"{LI}/in/meera-fx/", None, None),  # author URL on the enquiry
            (2, SEARCH, None, 1, None),  # via linkedin_profiles
            (3, SEARCH, None, None, 1),  # via the seen post's raw card
            (4, SEARCH, None, None, None),  # nothing better known: stays
            (5, f"{LI}/feed/update/urn:li:activity:7300000000000000001/", f"{LI}/in/x-fx/", None, None),  # a post: untouched
        ]
        for eid, url, author, pid, sid in rows:
            c.execute(text(
                "INSERT INTO enquiries (id, content_hash, raw_text, source_url, author_profile_url, author_profile_id, "
                "seen_post_id, date_added) VALUES (:i, :h, 'Synthetic post', :u, :a, :p, :s, :d)"
            ), {"i": eid, "h": f"h{eid}", "u": url, "a": author, "p": pid, "s": sid, "d": NOW})
            c.execute(text("INSERT INTO enquiry_sources (enquiry_id, source_ref, source_url, first_seen_at) VALUES (:i, :r, :u, :d)"),
                      {"i": eid, "r": f"ck:K{eid}", "u": SEARCH, "d": NOW})
    engine.dispose()


def test_upgrade_seeds_adds_columns_fixes_links_and_downgrades(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'q14.db'}"
    cfg = alembic_config(url)
    command.upgrade(cfg, PARENT)
    _seed_parent(url)
    command.upgrade(cfg, REV)
    command.upgrade(cfg, REV)  # re-runnable

    engine = build_engine(url)
    with engine.connect() as c:
        rules = c.execute(text("SELECT kind, text, position, active, source FROM triage_criteria ORDER BY id")).all()
        assert [(k, t) for k, t, *_ in rules] == [("include", t) for t in DEFAULT_INCLUDE] + [("exclude", t) for t in DEFAULT_EXCLUDE]
        assert {r[4] for r in rules} == {"seed"} and all(r[3] for r in rules)
        sets = c.execute(text("SELECT version, rules FROM triage_criteria_sets")).all()
        assert len(sets) == 1 and sets[0][0] == 1
        snap = sets[0][1] if isinstance(sets[0][1], list) else json.loads(sets[0][1])
        assert [r["kind"] for r in snap] == ["include"] * 5 + ["exclude"] * 8
        links = dict(c.execute(text("SELECT id, source_url FROM enquiries")).all())
        assert links == {
            1: f"{LI}/in/meera-fx/", 2: f"{LI}/in/asha-fx/", 3: f"{LI}/in/ravi-fx/", 4: SEARCH,
            5: f"{LI}/feed/update/urn:li:activity:7300000000000000001/",
        }
        sources = dict(c.execute(text("SELECT enquiry_id, source_url FROM enquiry_sources")).all())
        assert sources == {**links}
    engine.dispose()
    assert {"pipeline_prospect_id", "pipeline_company_id", "pipeline_match", "pipeline_run_id", "discard_source"} <= _cols(url, "enquiries")
    assert "posts_repeat_skipped" in _cols(url, "scrape_runs") and "criteria_version" in _cols(url, "llm_calls")

    command.downgrade(cfg, PARENT)
    assert "discard_source" not in _cols(url, "enquiries") and "criteria_version" not in _cols(url, "llm_calls")
    engine = build_engine(url)
    try:
        assert "triage_criteria" not in inspect(engine).get_table_names()
        with engine.connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM enquiries")).scalar() == 5  # rows survive the downgrade
    finally:
        engine.dispose()
    command.upgrade(cfg, REV)  # and back up again


def test_head_schema_has_no_drift(db_url: str) -> None:
    engine = build_engine(db_url)
    try:
        assert drift(engine) == []
    finally:
        engine.dispose()


def test_chain_has_one_head_containing_q14() -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    assert REV in {rev.revision for rev in script.walk_revisions("base", head_revision())}
