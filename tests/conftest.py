"""Shared test harness (scaffold).

* Every test gets an isolated runtime: ``SALES0_DATA_DIR`` and ``SALES0_DATABASE_URL``
  point into pytest's ``tmp_path`` (never the dev DB), and every singleton is reset
  before and after the test.
* The Anthropic key is read from the repo-root ``.env`` by ``get_settings()``. If any
  ``integration``/``e2e`` test is collected and the key is absent, the session FAILS
  (the gate is BLOCKED, not skipped).
* ``db_url`` returns a fresh SQLite file migrated to head with Alembic.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command

from sales0.config import get_settings, reset_settings
from sales0.db.migrate_check import alembic_config

KEY_MISSING_MESSAGE = "SALES0_ANTHROPIC_API_KEY missing in .env"

# Singletons with reset seams. Later slices' seams are picked up automatically when their
# module exists, so this file never has to change when a slice adds one.
_RESET_SEAMS = [
    ("sales0.config", "reset_settings"),
    ("sales0.db.engine", "reset_engine"),
    ("sales0.llm", "reset_llm_client"),
    ("sales0.jobs.runner", "reset_runner"),
]

# Fast, deterministic pacing + caps for tests (architecture.md → Conventions → Tests).
TEST_ENV = {
    "SALES0_LINKEDIN_MODE": "live",
    "SALES0_LOG_FORMAT": "json",
    "SALES0_LOG_LEVEL": "INFO",
    "SALES0_SCRAPE_DELAY_MIN_S": "0.05",
    "SALES0_SCRAPE_DELAY_MAX_S": "0.15",
    "SALES0_SCRAPE_KEYWORD_PAUSE_MIN_S": "0.05",
    "SALES0_SCRAPE_KEYWORD_PAUSE_MAX_S": "0.1",
    "SALES0_SCRAPE_MAX_RUNS_PER_DAY": "50",
}
_CLEARED_ENV = ("SALES0_LOG_FILE", "SALES0_HOST", "SALES0_PORT")


def reset_singletons() -> None:
    for module_name, func_name in _RESET_SEAMS:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        reset = getattr(module, func_name, None)
        if callable(reset):
            reset()


def _needs_real_key(item: pytest.Item) -> bool:
    parts = Path(str(item.path)).parts
    return "integration" in parts or "e2e" in parts or item.get_closest_marker("llm") is not None


def _is_e2e(item: pytest.Item) -> bool:
    return "e2e" in Path(str(item.path)).parts


def _browser_rank(item: pytest.Item) -> int:
    if _is_e2e(item):
        return 2
    return 1 if "browser_name" in getattr(item, "fixturenames", ()) else 0


@pytest.hookimpl(wrapper=True)
def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> Iterator[None]:
    if any(_needs_real_key(item) for item in items):
        reset_settings()
        present = get_settings().api_key_present
        reset_settings()
        if not present:
            pytest.exit(KEY_MISSING_MESSAGE, returncode=1)
    # Every other implementation (pytest's own fixture-based reordering, plugins, the e2e
    # conftest's marker hook) runs inside this wrapper; the final order is fixed after it.
    result = yield
    # pytest-playwright's ``page`` starts a session-scoped sync Playwright (an asyncio loop
    # on the main thread). Once it is up, every later in-process ``sync_playwright()`` (the
    # integration scraper/jobs tests) fails with "Sync API inside the asyncio loop". pytest's
    # fixture reordering anchors all ``[chromium]`` items where the first one sorts. So, for
    # ANY selection (tests/integration alone, tests/e2e alone, all of tests/): everything
    # else first, then the pytest-playwright tests, then E2E last.
    items[:] = sorted(items, key=_browser_rank)  # stable: keeps the order within each group
    return result


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SALES0_DATA_DIR", str(data_dir))
    test_url = os.environ.get("SALES0_TEST_DATABASE_URL") or f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("SALES0_DATABASE_URL", test_url)
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    for key in _CLEARED_ENV:
        monkeypatch.delenv(key, raising=False)
    reset_singletons()
    yield
    reset_singletons()


@pytest.fixture
def db_url() -> str:
    """A fresh database migrated to head; the app's settings already point at it."""
    url = get_settings().resolved_database_url
    command.upgrade(alembic_config(url), "head")
    return url
