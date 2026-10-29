"""Fixture LinkedIn test server: the fixture router in a uvicorn thread on a free port.

Scraper tests point ``SALES0_HOST``/``SALES0_PORT`` at it with ``SALES0_LINKEDIN_MODE=fixture``,
so ``scraper.urls.base_url`` resolves to it. A real LinkedIn account is never touched.
"""

from __future__ import annotations

import atexit
import shutil
import os
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

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
    """Settings in fixture mode pointed at the test server; profile under this test's tmp data dir.

    The browser channel (``chromium``) and the dead-port pin come from ``tests/conftest.py``.
    """
    monkeypatch.setenv("SALES0_LINKEDIN_MODE", "fixture")
    monkeypatch.setenv("SALES0_HOST", "127.0.0.1")
    monkeypatch.setenv("SALES0_PORT", str(fixture_linkedin_server))
    # The Phase 1.1-1.3 scraper tests assert one pass that ends at the first scroll without growth.
    # Phase 1.4 depth tests (test_scan_bulk.py) override these.
    monkeypatch.setenv("SALES0_SCRAPE_DATE_FILTERS", "past-week")
    monkeypatch.setenv("SALES0_SCRAPE_NO_GROWTH_SCROLLS", "1")
    # The fixture LinkedIn is static and answers at once, so the scraper's human-looking waits (jitter, polls,
    # scroll settle, lazy reveal, menu copy poll) only burn wall-clock. ONE knob scales them all together, ratios
    # preserved (fixture mode only; src/sales0/scraper/pacing.py). Override with SALES0_TEST_PACE_SCALE.
    monkeypatch.setenv("SALES0_SCRAPE_PACE_SCALE", os.environ.get("SALES0_TEST_PACE_SCALE", "0.2"))
    reset_settings()
    return get_settings()


_LOGIN_TEMPLATES: dict[tuple[str, str, str], Path] = {}  # (channel, mode, base url) -> logged-in profile dir


def _login_through_browser(settings: Settings) -> None:
    from sales0.scraper.browser import first_page, open_context
    from sales0.scraper.urls import base_url

    base = base_url(settings)
    with open_context(settings, headless=True) as context:
        page = first_page(context)
        page.goto(f"{base}/login?direct=1", wait_until="domcontentloaded")
        assert page.url == f"{base}/feed/", page.url
        assert any(c["name"] == "li_at" and c["value"] for c in context.cookies(base))


def log_in_profile(settings: Settings) -> None:
    """Log the fixture-mode profile in through the ``/login?direct=1`` test shortcut.

    Skips the 2FA pages (Connect's own tests walk those); the profile ends with a persistent
    ``li_at`` cookie exactly as after a completed Connect.

    The first call per process (and server/channel) really drives the browser through the login into a
    scratch data dir; every later call copies that closed, logged-in profile into this test's profile
    directory instead of launching Chromium again (~1.5-2 s per test).
    """
    from sales0.scraper.browser import profile_dir, resolve_browser
    from sales0.scraper.urls import base_url

    channel = resolve_browser(settings).channel
    key = (channel, settings.linkedin_mode, base_url(settings))
    template = _LOGIN_TEMPLATES.get(key)
    if template is None:
        root = Path(tempfile.mkdtemp(prefix="sales0-login-tpl-"))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        tpl_settings = settings.model_copy(update={"data_dir": str(root)})
        _login_through_browser(tpl_settings)
        template = _LOGIN_TEMPLATES[key] = profile_dir(tpl_settings, channel)
    dest = profile_dir(settings, channel)
    shutil.copytree(template, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("Singleton*", "lockfile"))


@pytest.fixture
def connected_settings(fixture_settings: Settings) -> Settings:
    """Fixture settings whose persistent profile is logged in (as after Connect)."""
    log_in_profile(fixture_settings)
    return fixture_settings
