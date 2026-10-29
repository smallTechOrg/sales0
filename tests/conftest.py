"""Shared test harness (scaffold; Phase 1.1 slice R6).

* Every test gets an isolated runtime: ``SALES0_DATA_DIR`` and ``SALES0_DATABASE_URL``
  point into pytest's ``tmp_path`` (never the dev DB), and every singleton is reset
  before and after the test.
* Port 8765 is the user's own sales0 server; no test may ever reach it.
  - ``SALES0_PORT`` is pinned, at session start and again per test, to a session-chosen
    DEAD port (no listener, so anything sent there is refused fast) unless the test sets
    its own server port afterwards.
  - A process-wide socket guard (every thread) refuses any loopback connect to port 8765,
    records it in ``guard_violations``, and the test that caused it FAILS, even when the
    code under test swallowed the error.
* Safety cool-downs default to 0 (the cool-down's own tests set them) and the browser
  channel defaults to ``chromium`` so the suite is deterministic on any machine.
* The Anthropic key is read from the repo-root ``.env`` by ``get_settings()``. Only RECORDING the LLM cassettes
  (``SALES0_TEST_LLM_REFRESH=1``) requires it (the session FAILS without one); ``llm`` integration tests replay
  cassettes (no key, no network) and the e2e journeys use a fake-LLM oracle except the real-Haiku ones, which skip
  themselves when no key is configured.
* ``db_url`` returns a fresh SQLite file migrated to head with Alembic.
* ``SALES0_TEST_POSTGRES_URL`` (e.g. ``postgresql+psycopg://postgres@127.0.0.1:55432/postgres``) runs the same suites on
  PostgreSQL: one template database is migrated to head once per session and every test gets its own
  ``CREATE DATABASE ... TEMPLATE`` copy (dropped afterwards), so tests stay as isolated as with a SQLite file.
"""

from __future__ import annotations

import errno
import importlib
import ipaddress
import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command

from sales0.config import get_settings, reset_settings
from sales0.db.migrate_check import alembic_config

pytest_plugins = ["tests.llm_cassette"]

KEY_MISSING_MESSAGE = "SALES0_ANTHROPIC_API_KEY missing in .env"

# Singletons with reset seams. Later slices' seams are picked up automatically when their
# module exists (a missing module or function is skipped).
_RESET_SEAMS = [
    ("sales0.config", "reset_settings"),
    ("sales0.db.engine", "reset_engine"),
    ("sales0.llm", "reset_llm_client"),
    ("sales0.jobs.runner", "reset_runner"),
    ("sales0.jobs.scheduler", "reset_scheduler"),
    ("sales0.scraper.browser", "reset_browser_cache"),
    ("sales0.services.team.connections", "reset_client_factory"),
    ("sales0.services.team.oauth", "reset_state_store"),
    ("sales0.jobs.reply_poller", "reset_poller"),
    ("sales0.services.events", "reset"),
    ("sales0.services.outreach.storage", "reset_storage"),
]

# Fast, deterministic pacing + caps for tests (architecture.md → Conventions → Tests).
TEST_ENV = {
    "SALES0_LINKEDIN_MODE": "live",
    "SALES0_AUTO_RESEARCH": "false",  # accept-triggered research opts in per test (no background AI calls elsewhere)
    "SALES0_RESEARCH_WEB_SEARCH": "false",  # q15: research tests opt in (web search mocked); keeps old fakes valid
    "SALES0_SCHEDULER_ENABLED": "false",  # no background thread; scheduler tests drive tick() themselves
    "SALES0_MONTHLY_SPEND_CAP_USD": "0",
    "SALES0_LOG_FORMAT": "json",
    "SALES0_LOG_LEVEL": "INFO",
    "SALES0_SCRAPE_DELAY_MIN_S": "0.05",
    "SALES0_SCRAPE_DELAY_MAX_S": "0.15",
    "SALES0_SCRAPE_KEYWORD_PAUSE_MIN_S": "0.05",
    "SALES0_SCRAPE_KEYWORD_PAUSE_MAX_S": "0.1",
    "SALES0_SCRAPE_MAX_RUNS_PER_DAY": "50",
    # Phase 1.1: cool-down off by default so safety tests run back to back; the cool-down's
    # own tests set explicit values.
    "SALES0_SAFETY_COOLDOWN_MIN": "0",
    "SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN": "0",
    # Phase 1.1: the bundled Chromium, so the suite never depends on an installed Chrome.
    # Real-Chrome tests (marker ``chrome``) set ``chrome`` explicitly.
    "SALES0_LINKEDIN_BROWSER_CHANNEL": "chromium",
    # Phase 1.4: tests seed their own keywords; concurrency pinned to the documented default.
    "SALES0_SEED_SUGGESTED_KEYWORDS": "false",
    "SALES0_TRIAGE_CONCURRENCY": "4",
    # Phase 5: no background reply poller in tests; they call poll_all()/run_cycle() explicitly.
    "SALES0_REPLY_POLL_MINUTES": "0",
    # Security: the Host allowlist (DNS-rebinding guard) also accepts Starlette TestClient's default Host
    # "testserver". Tests of the guard itself send an explicit foreign Host or turn this off.
    "SALES0_ALLOW_TESTSERVER_HOST": "true",
}
_CLEARED_ENV = ("SALES0_LOG_FILE", "SALES0_HOST", "SALES0_APP_PASSWORD")

# ----------------------------------------------------------------------- port isolation
# The user's own sales0. Tests never connect to it (socket guard) and never default to it.
FORBIDDEN_PORT = 8765
GUARD_MESSAGE = f"tests must never connect to {FORBIDDEN_PORT} (the user's sales0)"
# (host, port) of every refused connect attempt; checked (and cleared) around each test.
guard_violations: list[tuple[str, int]] = []

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_LOOPBACK_NAMES = {"localhost", "localhost.", "ip6-localhost", "ip6-loopback"}


def _is_loopback_host(host: object) -> bool:
    if not isinstance(host, str):
        return False
    name = host.strip().strip("[]").lower()
    if name in _LOOPBACK_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(name.split("%", 1)[0])  # drop an IPv6 zone id
    except ValueError:
        return False
    # 0.0.0.0 / :: reach the local host too; IPv4-mapped ::ffff:127.0.0.1 is loopback.
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip.is_loopback or ip.is_unspecified or bool(mapped and mapped.is_loopback)


def _forbidden(address: object) -> bool:
    return (
        isinstance(address, tuple)
        and len(address) >= 2
        and address[1] == FORBIDDEN_PORT
        and _is_loopback_host(address[0])
    )


def _guarded_connect(self: socket.socket, address: object) -> None:
    if _forbidden(address):
        guard_violations.append((str(address[0]), FORBIDDEN_PORT))  # type: ignore[index]
        raise ConnectionRefusedError(GUARD_MESSAGE)
    _real_connect(self, address)  # type: ignore[arg-type]


def _guarded_connect_ex(self: socket.socket, address: object) -> int:
    if _forbidden(address):
        guard_violations.append((str(address[0]), FORBIDDEN_PORT))  # type: ignore[index]
        return errno.ECONNREFUSED  # never 0
    return _real_connect_ex(self, address)  # type: ignore[arg-type]


def _pick_dead_port() -> int:
    """Bind 127.0.0.1:0, read the port, close: nothing listens there afterwards. The OS hands
    out ephemeral ports (49152+ on macOS), so it is never 8765."""
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port != FORBIDDEN_PORT:
            return port


DEAD_TEST_PORT = _pick_dead_port()
_guard_patch = pytest.MonkeyPatch()


def check_guard_violations() -> None:
    """Fail the current test if anything (any thread) tried to reach port 8765. Clears the list."""
    hits, guard_violations[:] = list(guard_violations), []
    if hits:
        pytest.fail(f"{GUARD_MESSAGE}; attempted: {hits}", pytrace=False)


def pytest_configure(config: pytest.Config) -> None:
    # Session start, before collection and before any Settings is built: pin the dead port
    # (env vars beat .env) and install the guard for every thread in this process.
    os.environ["SALES0_PORT"] = str(DEAD_TEST_PORT)
    _guard_patch.setattr(socket.socket, "connect", _guarded_connect)
    _guard_patch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)


def pytest_unconfigure(config: pytest.Config) -> None:
    _guard_patch.undo()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    # Checked right after the test body so the TEST is reported failed (not a teardown error)
    # when it - or code it called that swallowed the ConnectionRefusedError - hit 8765.
    result = yield
    check_guard_violations()
    return result


# ----------------------------------------------------------------------------- helpers
def reset_singletons() -> None:
    for module_name, func_name in _RESET_SEAMS:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        reset = getattr(module, func_name, None)
        if callable(reset):
            reset()


# ------------------------------------------------------------------ PostgreSQL per-test databases
_PG_ENV = "SALES0_TEST_POSTGRES_URL"
_pg_state: dict[str, object] = {}


def _pg_admin_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    return create_engine(os.environ[_PG_ENV], isolation_level="AUTOCOMMIT", poolclass=NullPool)


def _pg_url_for(dbname: str) -> str:
    from sqlalchemy.engine import make_url

    return make_url(os.environ[_PG_ENV]).set(database=dbname).render_as_string(hide_password=False)


def _pg_template_name() -> str:
    """Create (once per session) a database migrated to head; every test clones it."""
    name = _pg_state.get("template")
    if isinstance(name, str):
        return name
    from sqlalchemy import text

    name = f"sales0_tpl_{os.getpid()}"
    admin = _pg_admin_engine()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    command.upgrade(alembic_config(_pg_url_for(name)), "head")
    _pg_state["template"] = name
    return name


def _pg_create_test_db(tag: str, *, blank: bool = False) -> str:
    from sqlalchemy import text

    template = None if blank else _pg_template_name()
    name = f"sales0_t_{os.getpid()}_{tag}"[:60]
    admin = _pg_admin_engine()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else "")))
    admin.dispose()
    return name


def _pg_drop_db(name: str) -> None:
    from sqlalchemy import text

    admin = _pg_admin_engine()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    template = _pg_state.get("template")
    if isinstance(template, str) and os.environ.get(_PG_ENV):
        _pg_drop_db(template)


def _record_mode() -> bool:
    return os.environ.get("SALES0_TEST_LLM_REFRESH", "") in {"1", "true", "yes"}


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
    # Only recording needs a real key: llm integration tests replay cassettes (tests/llm_cassette.py) and the
    # e2e journeys use the fake-LLM oracle, except the few real-Haiku ones, which skip themselves without a key.
    if _record_mode() and any(item.get_closest_marker("llm") is not None for item in items):
        reset_settings()
        present = get_settings().api_key_present
        reset_settings()
        if not present:
            pytest.exit(KEY_MISSING_MESSAGE, returncode=1)
    # Every other implementation (pytest's own fixture-based reordering, plugins, the e2e
    # conftest's marker hook) runs inside this wrapper; the final order is fixed after it.
    result = yield
    if os.environ.get(_PG_ENV):
        skip_pg = pytest.mark.skip(reason="SQLite-specific; the PostgreSQL run covers the same behaviour elsewhere")
        for item in items:
            if item.get_closest_marker("sqlite_only"):
                item.add_marker(skip_pg)
    # pytest-playwright's ``page`` starts a session-scoped sync Playwright (an asyncio loop
    # on the main thread). Once it is up, every later in-process ``sync_playwright()`` (the
    # integration scraper/jobs tests) fails with "Sync API inside the asyncio loop". pytest's
    # fixture reordering anchors all ``[chromium]`` items where the first one sorts. So, for
    # ANY selection (tests/integration alone, tests/e2e alone, all of tests/): everything
    # else first, then the pytest-playwright tests, then E2E last.
    items[:] = sorted(
        items, key=lambda item: (_browser_rank(item), 0 if "bulk_journey" in Path(str(item.path)).name else 1)
    )  # stable: keeps the order within each group
    return result


@pytest.fixture(scope="session")
def dead_test_port() -> int:
    """The session's default ``SALES0_PORT``: a loopback port with no listener."""
    return DEAD_TEST_PORT


@pytest.fixture(autouse=True)
def _isolated_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[None]:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SALES0_DATA_DIR", str(data_dir))
    pg_db: str | None = None
    if os.environ.get(_PG_ENV):
        # ``@pytest.mark.unmigrated``: the test wants a database that was never migrated (a blank one, not a template copy)
        pg_db = _pg_create_test_db(
            f"{abs(hash(tmp_path)) % 10**9}", blank=request.node.get_closest_marker("unmigrated") is not None
        )
        test_url = _pg_url_for(pg_db)
    else:
        test_url = (
            os.environ.get("SALES0_TEST_DATABASE_URL")
            or f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
        )
    monkeypatch.setenv("SALES0_DATABASE_URL", test_url)
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    for key in _CLEARED_ENV:
        monkeypatch.delenv(key, raising=False)
    # Never the default 8765 (overrides .env too). A test that runs a real server sets its
    # own SALES0_PORT afterwards; monkeypatch restores the dead port after it.
    monkeypatch.setenv("SALES0_PORT", str(DEAD_TEST_PORT))
    guard_violations.clear()
    reset_singletons()
    yield
    # Undo the test's own monkeypatches (env AND attributes, e.g. a stubbed
    # ``browser.system_locale``) first, so the reset seams run against the real modules.
    monkeypatch.undo()
    reset_singletons()
    if pg_db is not None:
        _pg_drop_db(pg_db)
    # Catch-all for fixture setup/teardown connects (the test body is checked in
    # pytest_runtest_call).
    check_guard_violations()


@pytest.fixture(scope="session")
def sqlite_template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A SQLite file migrated to head ONCE per session (per xdist worker: each has its own basetemp).

    ``alembic upgrade head`` costs ~0.7-1.3 s; copying this file costs ~2 ms. ``db_url`` copies it into the
    test's own DB path, so every test still owns a private, fully migrated file."""
    path = tmp_path_factory.mktemp("template") / "template.db"
    command.upgrade(alembic_config(f"sqlite:///{path.as_posix()}"), "head")
    return path


def _copy_sqlite(src: Path, dst: Path) -> None:
    import sqlite3

    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(src))
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)  # replaces the destination's content atomically; copies WAL state safely
    finally:
        target.close()
        source.close()


@pytest.fixture
def db_url(request: pytest.FixtureRequest) -> str:
    """A fresh database migrated to head; the app's settings already point at it.

    SQLite: a copy of the per-session migrated template (no Alembic run). PostgreSQL: the per-test
    ``CREATE DATABASE ... TEMPLATE`` clone is already migrated. A test that needs the real
    ``alembic upgrade`` path (migration tests) calls ``command.upgrade`` itself on its own URL."""
    from sqlalchemy.engine import make_url

    url = get_settings().resolved_database_url
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database and not os.environ.get("SALES0_TEST_DATABASE_URL") and not os.environ.get("SALES0_TEST_MIGRATE_EACH"):
        _copy_sqlite(request.getfixturevalue("sqlite_template_db"), Path(parsed.database))
        return url
    if os.environ.get(_PG_ENV) and request.node.get_closest_marker("unmigrated") is None:
        return url  # cloned from the migrated template
    command.upgrade(alembic_config(url), "head")
    return url


# ------------------------------------------------------------------------------ fast helpers
@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Opt in: ``time.sleep`` returns immediately (calls are recorded in the returned list).

    Only patches ``time.sleep``; code that takes an injected ``sleep=`` callable should be given
    ``lambda _s: None`` directly. Do not use in tests that wait on a thread with ``time.sleep``."""
    import time as _time

    calls: list[float] = []
    monkeypatch.setattr(_time, "sleep", lambda seconds=0: calls.append(float(seconds)))
    return calls


@pytest.fixture
def fake_clock():
    """Opt in: a controllable clock (``clock.now()``, ``clock.sleep(s)`` advances it, ``clock.advance(...)``)."""
    from tests.support.clock import FakeClock

    return FakeClock()
