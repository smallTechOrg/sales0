"""One real Haiku call: the digest summary generated from seeded data."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import LlmCall
from sales0.llm import get_llm_client
from sales0.services import digest as dg
from tests.integration.digest.conftest import NOW, Team, add_enquiry, add_message, add_run, hours_ago

pytestmark = pytest.mark.llm


def test_real_haiku_writes_a_grounded_summary(db: Session, team: Team) -> None:
    add_enquiry(db, added=hours_ago(1), company="Acme Logistics", summary="GenAI workshop for 40 managers in Pune")
    add_enquiry(db, added=hours_ago(3), company="Beta Corp", priority="medium", summary="Prompt engineering bootcamp")
    add_message(db, team.bob, state="failed", updated_at=hours_ago(2), error="Gmail rejected the message", subject="Hello Beta")
    add_run(db, started=hours_ago(5), posts_new=4, errors=1)
    view = dg.generate_digest(db, now=NOW, llm=get_llm_client(), with_summary=True)
    assert view.summary_text, view.notes
    lines = view.summary_text.splitlines()
    assert 1 <= len(lines) <= 5
    text = view.summary_text.lower()
    assert "acme" in text or "enquir" in text or "failed" in text
    row = db.scalars(select(LlmCall).where(LlmCall.feature == "suggest")).one()
    assert row.status == "ok" and row.cost_usd_micros > 0 and row.prompt_version == "digest-v1"
    assert view.summary_cost_usd_micros == row.cost_usd_micros
    assert dg.get_digest(db, view.id).summary_text == view.summary_text
