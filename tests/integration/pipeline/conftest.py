"""Pipeline tests: the real discovery job + real headless Chromium against the fixture LinkedIn's
bulk corpus, with the fake-LLM oracle (``bulk.expected_verdict``) standing in for Haiku."""

from __future__ import annotations

import os

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from sales0.config import Settings, reset_settings
from sales0.db.engine import session_scope
from tests.integration.scraper.conftest import (  # noqa: F401
    connected_settings,
    fixture_linkedin_server,
    fixture_settings,
)
from tests.integration.services.helpers import set_session_status


@pytest.fixture
def db(db_url: str) -> Iterator[Session]:
    with session_scope() as session:
        yield session


@pytest.fixture
def depth(db_url: str, connected_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:  # noqa: F811
    """Phase 1.4 depth: two date passes, 2 idle scrolls, 6 keywords per run, signed-in fixture profile."""
    monkeypatch.setenv("SALES0_SCRAPE_DATE_FILTERS", "past-week,past-month")
    monkeypatch.setenv("SALES0_SCRAPE_NO_GROWTH_SCROLLS", "2")
    monkeypatch.setenv("SALES0_SCRAPE_MAX_KEYWORDS_PER_RUN", "6")
    # Speed only: the fixture serves each bulk page from localhost in milliseconds, so the scraper's human-like
    # waits (settle window, polls, delays, lazy-reveal) only burn wall time. One knob scales them all together
    # (ratios preserved); it is honoured only in fixture mode. Behaviour is unchanged (same polls, same
    # N idle scrolls to stop). The 60-80 posts/keyword assertions hold at this scale (see tests/README.md).
    monkeypatch.setenv("SALES0_SCRAPE_PACE_SCALE", os.environ.get("SALES0_TEST_PACE_SCALE", "0.2"))
    reset_settings()
    set_session_status("connected")
    return connected_settings.__class__()
