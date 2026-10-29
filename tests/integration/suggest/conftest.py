"""Suggestion fixtures: a migrated SQLite DB seeded with keywords, runs, genuine enquiries and hits."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from sales0.clock import utcnow
from sales0.config import reset_settings
from sales0.db.engine import get_sessionmaker
from sales0.db.models import (
    Enquiry,
    EnquiryKeywordHit,
    Keyword,
    ScrapeRun,
    ScrapeRunKeyword,
)
from sales0.services.dedupe import content_hash

# Invented companies/requirements only (no real personal data).
TEXTS = [
    "Zentora Foods is looking for a prompt engineering workshop for 30 sales managers in Pune.",
    "Brightwell Logistics needs a corporate trainer for ChatGPT and Copilot adoption across operations teams.",
    "Our Kovalam Bank academy wants a generative AI bootcamp for relationship managers, on-site in Chennai.",
    "Hiring a vendor for AI upskilling of 200 engineers at Quillon Software; LLM application development training.",
    "Northgate Pharma seeks a data literacy and machine learning course for its analytics team.",
    "Orbis Retail wants a workshop on AI for marketing content creation for 25 marketers.",
    "Tamarind Hotels: need trainer for AI chatbot building for guest services staff.",
    "Veridian Telecom is looking for generative AI leadership training for senior managers.",
    "Looking for a RAG and vector database training partner for our Halcyon Insurance data team.",
    "Mistral Textiles needs AI automation workshop (no-code) for the finance department.",
    "Ardent Schools wants teacher training on using AI tools in classrooms, 60 teachers.",
    "Pinecrest Auto needs prompt engineering and AI agents training for its R&D group.",
]


@dataclass
class Seed:
    kw: dict[str, int] = field(default_factory=dict)
    enquiry_ids: list[int] = field(default_factory=list)


@pytest.fixture
def db(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    reset_settings()
    with get_sessionmaker()() as session:
        yield session


def add_keyword(db: Session, text: str, *, active: bool = True) -> int:
    k = Keyword(text=text, text_key=text.lower(), active=active)
    db.add(k)
    db.flush()
    return k.id


def add_run_stat(
    db: Session, keyword_id: int | None, text: str, seen: int, *, age_days: int = 1
) -> int:
    run = ScrapeRun(
        trigger="manual",
        mode="live",
        status="succeeded",
        started_at=utcnow() - timedelta(days=age_days),
        finished_at=utcnow(),
    )
    db.add(run)
    db.flush()
    db.add(
        ScrapeRunKeyword(
            run_id=run.id,
            keyword_id=keyword_id,
            keyword_text=text,
            status="done",
            posts_seen=seen,
        )
    )
    db.flush()
    return run.id


def add_enquiry(
    db: Session,
    text: str,
    *,
    verdict: str | None = "genuine",
    hits: list[tuple[int | None, str]] = (),
    run_id: int | None = None,
) -> int:  # type: ignore[assignment]
    e = Enquiry(
        content_hash=content_hash(text),
        raw_text=text,
        ai_verdict=verdict,
        triage_status="done",
    )
    db.add(e)
    db.flush()
    for kid, ktext in hits:
        db.add(
            EnquiryKeywordHit(
                enquiry_id=e.id, run_id=run_id, keyword_id=kid, keyword_text=ktext
            )
        )
    db.flush()
    return e.id


@pytest.fixture
def seeded(db: Session) -> Seed:
    """Keywords: 'ai workshop' (60 scanned, 12 genuine), 'excel training' (40 scanned, 0: DEAD),
    'python bootcamp' (10 scanned, 0: too little data), 'old paused' (inactive, 80 scanned, 0)."""
    s = Seed()
    for text, active in (
        ("ai workshop", True),
        ("excel training", True),
        ("python bootcamp", True),
        ("old paused", False),
    ):
        s.kw[text] = add_keyword(db, text, active=active)
    run1 = add_run_stat(db, s.kw["ai workshop"], "ai workshop", 40)
    add_run_stat(db, s.kw["ai workshop"], "ai workshop", 20)
    add_run_stat(db, s.kw["excel training"], "excel training", 25)
    add_run_stat(db, s.kw["excel training"], "excel training", 15)
    add_run_stat(db, s.kw["python bootcamp"], "python bootcamp", 10)
    add_run_stat(db, s.kw["old paused"], "old paused", 80)
    for t in TEXTS:
        s.enquiry_ids.append(
            add_enquiry(db, t, hits=[(s.kw["ai workshop"], "ai workshop")], run_id=run1)
        )
    db.commit()
    return s


@pytest.fixture
def web(db_url: str):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from sales0.web.app import create_app

    with TestClient(create_app()) as c:
        yield c
