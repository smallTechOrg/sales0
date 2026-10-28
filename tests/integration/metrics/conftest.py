from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from sales0.db.engine import session_scope


@pytest.fixture
def db(db_url: str) -> Iterator[Session]:
    with session_scope() as session:
        yield session
