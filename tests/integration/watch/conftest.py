from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from sales0.db.engine import session_scope
from sales0.services import events

# The fixture-LinkedIn server fixtures (uvicorn thread, fixture mode).
from tests.integration.scraper.conftest import (  # noqa: F401
    _fast_fixture_waits,
    connected_settings,
    fixture_linkedin_server,
    fixture_settings,
)


@pytest.fixture
def db(db_url: str) -> Iterator[Session]:
    with session_scope() as session:
        yield session


@pytest.fixture(autouse=True)
def _clean_events() -> Iterator[None]:
    events.reset()
    yield
    events.reset()
