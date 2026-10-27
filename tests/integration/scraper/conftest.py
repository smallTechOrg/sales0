"""Fixture LinkedIn test server: the fixture router in a uvicorn thread on a free port.

Scraper tests point ``SALES0_HOST``/``SALES0_PORT`` at it with ``SALES0_LINKEDIN_MODE=fixture``,
so ``scraper.urls.base_url`` resolves to it. A real LinkedIn account is never touched.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn

from sales0.config import Settings, get_settings, reset_settings
from sales0.linkedin_fixture import build_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def fixture_linkedin_server() -> Iterator[int]:
    """Yields the port of a running fixture LinkedIn server (session-scoped)."""
    port = _free_port()
    config = uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="fixture-linkedin", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("fixture LinkedIn server did not start")
        time.sleep(0.05)
    yield port
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def fixture_settings(fixture_linkedin_server: int, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings in fixture mode pointed at the test server; profile under this test's tmp data dir."""
    monkeypatch.setenv("SALES0_LINKEDIN_MODE", "fixture")
    monkeypatch.setenv("SALES0_HOST", "127.0.0.1")
    monkeypatch.setenv("SALES0_PORT", str(fixture_linkedin_server))
    monkeypatch.setenv("SALES0_LINKEDIN_HEADLESS", "false")  # fixture mode must still run headless
    reset_settings()
    return get_settings()


@pytest.fixture
def connected_settings(fixture_settings: Settings) -> Settings:
    """Fixture settings whose persistent profile has completed Connect."""
    from sales0.scraper.connect import connect_interactive

    result = connect_interactive(fixture_settings, on_tick=lambda _s: None, should_cancel=lambda: False)
    assert result.connected, result
    return fixture_settings
