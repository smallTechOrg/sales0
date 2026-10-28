"""E2E fixtures: a live ``python -m sales0`` subprocess (fixture LinkedIn, port 8766, its
own temp SQLite + data dir) driven by pytest-playwright's headless Chromium ``page``.

Every test here gets the ``e2e``/``browser``/``llm`` markers and the 600s e2e timeout
(architecture.md → Conventions → Tests)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page

from tests.support.live_server import LiveServer, live_server

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
            if item.get_closest_marker("timeout") is None:
                item.add_marker(pytest.mark.timeout(E2E_TIMEOUT_S))


def _free_port(start: int = 8766) -> int:
    """First free loopback port >= 8766 (other builders/suites may hold 8766); never 8765."""
    import socket

    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port for the e2e server")


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with live_server(tmp_path / "e2e", port=_free_port()) as srv:
        yield srv


@pytest.fixture
def ui(page: Page) -> Page:
    """The headless page with timeouts suited to a live server + real Haiku."""
    page.set_default_timeout(20_000)
    page.set_default_navigation_timeout(30_000)
    return page
