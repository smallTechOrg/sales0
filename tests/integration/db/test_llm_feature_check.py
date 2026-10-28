"""The migrated llm_calls.feature CHECK allows every feature either branch uses (merge guard)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from sales0.db.engine import build_engine
from sales0.db.models.enums import LLM_FEATURES

_INSERT = text(
    "INSERT INTO llm_calls (feature, model, status, input_tokens, output_tokens, "
    "cost_usd_micros, latency_ms, fewshot_count, created_at) "
    "VALUES (:f, 'm', 'ok', 0, 0, 0, 0, 0, '2026-01-01 00:00:00')"
)


def test_every_allowed_feature_inserts_and_unknown_is_rejected(db_url: str) -> None:
    assert {"ping", "triage", "research", "drafting", "suggest"} <= set(LLM_FEATURES)
    engine = build_engine(db_url)
    try:
        with engine.begin() as conn:
            for feature in LLM_FEATURES:
                conn.execute(_INSERT, {"f": feature})
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(_INSERT, {"f": "bogus"})
    finally:
        engine.dispose()
