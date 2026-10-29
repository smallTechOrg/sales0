"""Fixtures for the CRM service tests: a migrated temp SQLite DB, the hooks installed, builders."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from sales0.db.engine import session_scope
from sales0.services import crm, events


@pytest.fixture
def db(db_url: str) -> Iterator[Session]:
    """A session on a fresh migrated SQLite file (committed at the end)."""
    with session_scope() as session:
        yield session


@pytest.fixture
def installed() -> Iterator[None]:
    """The CRM hooks subscribed (``events.reset()`` in the harness removes them between tests)."""
    events.reset()
    crm.install()
    yield
    events.reset()
