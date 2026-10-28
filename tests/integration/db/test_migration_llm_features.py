"""The llm_calls.feature CHECK accepts research/drafting/suggest after p3_outreach and narrows back on downgrade."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config

INSERT = "INSERT INTO llm_calls (feature, model, status, created_at) VALUES (:f, 'm', 'ok', '2026-01-01 00:00:00')"


def test_new_features_insert_and_unknown_is_still_rejected(db_url: str) -> None:
    engine = build_engine(db_url)
    try:
        with engine.begin() as conn:
            for feature in ("ping", "triage", "research", "drafting", "suggest"):
                conn.execute(text(INSERT), {"f": feature})
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text(INSERT), {"f": "bogus"})
    finally:
        engine.dispose()


def test_downgrade_restores_the_narrow_check_and_drops_new_rows(db_url: str) -> None:
    cfg = alembic_config(db_url)
    engine = build_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(INSERT), {"f": "research"})
            conn.execute(text(INSERT), {"f": "triage"})
        command.downgrade(cfg, "0002_phase1_discovery")
        with engine.begin() as conn:
            assert [r[0] for r in conn.execute(text("SELECT feature FROM llm_calls"))] == ["triage"]
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text(INSERT), {"f": "research"})
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text(INSERT), {"f": "drafting"})
    finally:
        engine.dispose()
