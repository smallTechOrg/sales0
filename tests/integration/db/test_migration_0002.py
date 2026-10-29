"""Migration 0002_phase1_discovery on a real SQLite file: head revision, exact seeds,
no drift between the ORM models and the migrated schema, and a clean down/up round-trip."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from alembic import command
from alembic.util.exc import CommandError
from sqlalchemy import inspect, select

from sales0.config import REPO_ROOT
from sales0.db.engine import build_engine, session_scope
from sales0.db.migrate_check import alembic_config, current_revision, head_revision
from tests.integration.db._drift import assert_inline_fks_cascade, drift
from sales0.db.models import AppMeta, LinkedInSession, Offering, OfferingGroup

# "users" is retired by p7 (team members are the users); 0002 still creates it and 0002's downgrade drops it.
PHASE1_TABLES = {
    "offering_groups",
    "offerings",
    "keywords",
    "linkedin_session",
    "scrape_runs",
    "scrape_run_keywords",
    "enquiries",
    "enquiry_sources",
    "enquiry_keyword_hits",
    "triage_decisions",
    "llm_calls",
    "status_history",
}

EXPECTED_OFFERINGS = [
    (
        1,
        "Training",
        "AI corporate workshops",
        "Instructor-led AI/GenAI awareness and productivity workshops for company teams, managers "
        "and leadership, online or on-site.",
    ),
    (
        2,
        "Training",
        "Technical AI training",
        'Hands-on technical programs for engineers, e.g. "Build your first AI agent", Claude Code, '
        "LLM app development.",
    ),
    (
        3,
        "Services",
        "AI consulting & build services",
        "Consulting, design and build of AI solutions: assistants/chatbots, agents, automation, RAG "
        "over company documents.",
    ),
    (
        4,
        "Products",
        "Products",
        "smallTech's AI products and tools offered to companies.",
    ),
]


def _tables(url: str) -> set[str]:
    engine = build_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_0002_is_an_ancestor_of_head_and_db_is_at_head(db_url: str) -> None:
    assert head_revision()  # never hard-code the head (see NOTES.md)
    engine = build_engine(db_url)
    try:
        assert current_revision(engine) == head_revision()
    finally:
        engine.dispose()
    assert PHASE1_TABLES | {"app_meta", "alembic_version"} <= _tables(db_url)


def test_migration_seeds_exactly_the_four_default_offerings_in_three_groups(
    db_url: str,
) -> None:
    with session_scope() as db:
        groups = (
            db.execute(select(OfferingGroup).order_by(OfferingGroup.id)).scalars().all()
        )
        assert [(g.id, g.name, g.name_key, g.sort_order) for g in groups] == [
            (1, "Training", "training", 1),
            (2, "Services", "services", 2),
            (3, "Products", "products", 3),
        ]
        offerings = db.execute(select(Offering).order_by(Offering.id)).scalars().all()
        assert [
            (o.id, o.group.name, o.name, o.description) for o in offerings
        ] == EXPECTED_OFFERINGS
        assert all(o.active for o in offerings)
        assert [o.sort_order for o in offerings] == [1, 2, 3, 4]
        assert [o.name_key for o in offerings] == [
            "ai corporate workshops",
            "technical ai training",
            "ai consulting & build services",
            "products",
        ]
        assert all(
            o.created_at is not None and o.updated_at is not None for o in offerings
        )


def test_migration_seeds_the_linkedin_session_singleton_as_not_connected(
    db_url: str,
) -> None:
    with session_scope() as db:
        rows = db.execute(select(LinkedInSession)).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.id == 1 and row.status == "not_connected"
        assert row.last_checked_at is None and row.last_connected_at is None
        assert row.last_successful_run_at is None and row.last_error is None
        assert row.updated_at is not None and row.updated_at.tzinfo is not None


def test_migrated_schema_matches_the_orm_models_with_no_drift(db_url: str) -> None:
    engine = build_engine(db_url)
    try:
        assert drift(engine) == []
        assert_inline_fks_cascade(engine)
    finally:
        engine.dispose()


def test_downgrade_to_baseline_drops_phase1_tables_and_upgrade_reseeds_once(
    db_url: str,
) -> None:
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "0001_baseline")
    remaining = _tables(db_url)
    assert remaining & PHASE1_TABLES == set()
    assert "app_meta" in remaining

    command.upgrade(cfg, "head")
    assert PHASE1_TABLES <= _tables(db_url)
    with session_scope() as db:
        assert len(db.execute(select(Offering)).scalars().all()) == 4
        assert len(db.execute(select(OfferingGroup)).scalars().all()) == 3
        assert len(db.execute(select(LinkedInSession)).scalars().all()) == 1
        assert db.get(AppMeta, "schema_note") is not None


def test_downgrade_to_base_removes_everything_but_the_version_table(
    db_url: str,
) -> None:
    command.downgrade(alembic_config(db_url), "base")
    assert _tables(db_url) <= {"alembic_version"}


def test_upgrade_head_is_idempotent_when_already_at_head(db_url: str) -> None:
    command.upgrade(alembic_config(db_url), "head")
    with session_scope() as db:
        assert len(db.execute(select(Offering)).scalars().all()) == 4


def test_alembic_cli_upgrade_and_current_print_the_head_revision(
    tmp_path: Path,
) -> None:
    """The documented commands, run as the user runs them, against a fresh SQLite file."""
    url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    full_env = {**os.environ, "SALES0_DATABASE_URL": url}
    up = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=full_env,
        capture_output=True,
        text=True,
    )
    assert up.returncode == 0, up.stderr
    cur = subprocess.run(
        ["uv", "run", "alembic", "current"],
        cwd=REPO_ROOT,
        env=full_env,
        capture_output=True,
        text=True,
    )
    assert cur.returncode == 0, cur.stderr
    assert f"{head_revision()} (head)" in cur.stdout


def test_upgrade_fails_loudly_on_an_unknown_revision(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'bad.db').as_posix()}"
    with pytest.raises(CommandError, match="9999_does_not_exist"):
        command.upgrade(alembic_config(url), "9999_does_not_exist")
