"""Migration q15_research_7d3a9c21: new tables, briefs -> research versions, notes -> first note entry, llm_calls
web-search column + 'summary' feature; retry-safe; downgrade; no drift. Synthetic rows only. Runs on PostgreSQL too
(``SALES0_TEST_POSTGRES_URL``) through the shared ``db_url`` fixture when set."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config
from tests.integration.db._drift import drift

PARENT = "q14_criteria_5b8e1f3a"
REV = "q15_research_7d3a9c21"
NOW = "2026-10-01 00:00:00"


def _seed(url: str) -> None:
    engine = build_engine(url)
    with engine.begin() as c:
        for pid, notes in ((1, "Met at expo; prefers WhatsApp"), (2, None), (3, "   ")):
            c.execute(text("INSERT INTO prospects (id, name, notes, created_at, updated_at) VALUES (:i, :n, :notes, :d, :d)"),
                      {"i": pid, "n": f"Prospect {pid}", "notes": notes, "d": NOW})
        briefs = [
            (1, 1, {"company_name": "Acme", "what_they_do": "Freight", "sources": ["https://acme.test/"],
                    "research_status": "partial", "research_error": "thin"}, ["https://acme.test/"], "2026-10-01 00:00:00"),
            (2, 1, {"company_name": "Acme", "what_they_do": "Freight v2"}, [], "2026-10-02 00:00:00"),
            (3, None, {"company_name": "Loose"}, [], "2026-10-03 00:00:00"),  # no prospect: stays a compose-only brief
        ]
        for bid, pid, data, sources, created in briefs:
            c.execute(text(
                "INSERT INTO company_briefs (id, company_name, brief_json, sources_json, created_at, cost_usd_micros, prospect_id) "
                "VALUES (:i, 'Acme', :b, :s, :d, 700, :p)"
            ), {"i": bid, "b": json.dumps(data), "s": json.dumps(sources), "d": created, "p": pid})
        c.execute(text("INSERT INTO llm_calls (feature, model, status, created_at) VALUES ('research', 'm', 'ok', :d)"), {"d": NOW})
    engine.dispose()


def test_upgrade_backfills_is_retry_safe_and_downgrades(db_url: str, tmp_path: Path) -> None:
    url = db_url if not db_url.startswith("sqlite") else f"sqlite:///{tmp_path / 'q15.db'}"
    cfg = alembic_config(url)
    command.downgrade(cfg, PARENT) if not url.endswith("q15.db") else command.upgrade(cfg, PARENT)
    _seed(url)
    command.upgrade(cfg, REV)
    command.upgrade(cfg, REV)  # re-runnable
    engine = build_engine(url)
    try:
        with engine.connect() as c:
            docs = c.execute(text("SELECT prospect_id, doc_type, version, origin, status, error, company_brief_id, data_json, "
                                  "sources_json, cost_usd_micros FROM prospect_docs ORDER BY id")).all()
            assert [(d[0], d[1], d[2], d[3], d[4], d[6]) for d in docs] == [
                (1, "research", 1, "ai", "partial", 1), (1, "research", 2, "ai", "done", 2)]
            assert docs[0][5] == "thin" and "research_status" not in json.loads(docs[0][7]) and docs[0][9] == 700
            assert json.loads(docs[0][8]) == [{"kind": "website", "label": "Company website", "url": "https://acme.test/"}]
            notes = c.execute(text("SELECT prospect_id, body, author, source FROM prospect_notes")).all()
            assert notes == [(1, "Met at expo; prefers WhatsApp", "team", "migrated")]
            assert c.execute(text("SELECT notes FROM prospects WHERE id = 1")).scalar() == "Met at expo; prefers WhatsApp"  # kept, legacy
            assert c.execute(text("SELECT web_search_requests FROM llm_calls")).scalar() == 0
        with engine.begin() as c:
            c.execute(text("INSERT INTO llm_calls (feature, model, status, created_at, web_search_requests) "
                           "VALUES ('summary', 'm', 'ok', :d, 2)"), {"d": NOW})
        with pytest.raises(IntegrityError), engine.begin() as c:
            c.execute(text("INSERT INTO prospect_docs (prospect_id, doc_type, version, origin, created_at) "
                           "VALUES (1, 'research', 1, 'ai', :d)"), {"d": NOW})
        assert drift(engine) == []

        command.downgrade(cfg, PARENT)
        insp = inspect(engine)
        assert not {"prospect_docs", "prospect_notes", "worker_tasks"} & set(insp.get_table_names())
        assert "web_search_requests" not in {col["name"] for col in insp.get_columns("llm_calls")}
        with engine.connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM llm_calls WHERE feature = 'research'")).scalar() == 2  # summary kept as research
            assert c.execute(text("SELECT COUNT(*) FROM company_briefs")).scalar() == 3
        command.upgrade(cfg, "head")
        with engine.connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM prospect_docs")).scalar() == 2
        assert drift(engine) == []
    finally:
        engine.dispose()
