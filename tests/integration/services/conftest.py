from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from sales0.db.engine import session_scope


@pytest.fixture
def db(db_url: str) -> Iterator[Session]:
    """A session on a fresh migrated SQLite file; committed at the end of the test."""
    with session_scope() as session:
        yield session
