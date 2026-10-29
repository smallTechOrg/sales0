"""Import fixtures: a migrated SQLite DB session and the synthetic CSV fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from sales0.db.engine import get_sessionmaker

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "import"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def db(db_url: str) -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session
