from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from sales0.db.engine import session_scope

# Slice C's fixture-LinkedIn server fixtures (uvicorn thread on a free port; fixture mode).
from tests.integration.scraper.conftest import (  # noqa: F401
    connected_settings,
    fixture_linkedin_server,
    fixture_settings,
)


@pytest.fixture
def db(db_url: str) -> Iterator[Session]:
    with session_scope() as session:
        yield session
