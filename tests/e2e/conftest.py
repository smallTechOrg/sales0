"""E2E fixtures: a live ``python -m sales0`` subprocess (fixture LinkedIn, port 8766, its
own temp SQLite + data dir) driven by pytest-playwright's headless Chromium ``page``.

Every test here gets the ``e2e``/``browser``/``llm`` markers and the 600s e2e timeout
(architecture.md → Conventions → Tests)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page

from tests.e2e._fast import free_port, live_server
from tests.support.live_server import LiveServer

E2E_TIMEOUT_S = 600


def _is_e2e(item: pytest.Item) -> bool:
    return "e2e" in Path(str(item.path)).parts


@pytest.hookimpl(tryfirst=True)  # before `-m` deselection sees the items
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Markers only. Ordering (browser tests after in-process Playwright tests, E2E last)
    # is owned by the root tests/conftest.py wrapper so it applies to every selection.
    for item in items:
        if _is_e2e(item):
            item.add_marker(pytest.mark.e2e)
            item.add_marker(pytest.mark.browser)
            item.add_marker(pytest.mark.llm)
            if "bulk_journey" in item.path.name:
                # the longest journey: its own xdist worker (never queued behind others), scheduled first
                item.add_marker(pytest.mark.xdist_group("e2e-bulk"))
            if item.get_closest_marker("timeout") is None:
                item.add_marker(pytest.mark.timeout(E2E_TIMEOUT_S))


_free_port = free_port  # re-exported for the journeys


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with live_server(tmp_path / "e2e") as srv:
        yield srv


@pytest.fixture
def real_llm_server(tmp_path: Path) -> Iterator[LiveServer]:
    """The full-stack smoke: the real Haiku key from ``.env``."""
    from sales0.config import get_settings, reset_settings

    reset_settings()
    present = get_settings().api_key_present
    reset_settings()
    if not present:
        pytest.skip("real-Haiku journey: SALES0_ANTHROPIC_API_KEY not configured")
    with live_server(tmp_path / "e2e", real_llm=True) as srv:
        yield srv


@pytest.fixture
def ui(page: Page) -> Page:
    """The headless page with timeouts suited to a live server + real Haiku."""
    page.set_default_timeout(20_000)
    page.set_default_navigation_timeout(30_000)
    return page
