"""The test harness never targets the user's sales0 (port 8765) or the dev database.

Port 8765 is the default port and the user may be running their own server there. The root
conftest pins ``SALES0_PORT`` to a session-chosen dead port and installs a process-wide
socket guard that refuses (records, and fails the test on) any loopback connect to 8765.
"""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

import httpx
import pytest

from sales0.config import REPO_ROOT, get_settings, reset_settings
from tests.conftest import (
    DEAD_TEST_PORT,
    FORBIDDEN_PORT,
    check_guard_violations,
    guard_violations,
)

DEV_DB = REPO_ROOT / "data" / "sales0.db"


def test_effective_port_is_never_the_users_server_port() -> None:
    settings = get_settings()
    assert FORBIDDEN_PORT == 8765
    assert settings.port != FORBIDDEN_PORT
    assert settings.port == DEAD_TEST_PORT
    assert f":{FORBIDDEN_PORT}" not in settings.base_url


def test_port_is_pinned_in_the_process_environment_from_session_start() -> None:
    assert os.environ["SALES0_PORT"] == str(DEAD_TEST_PORT)


def test_fixture_mode_switch_without_a_port_still_uses_the_dead_test_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SALES0_LINKEDIN_MODE", "fixture")
    reset_settings()
    settings = get_settings()
    assert settings.linkedin_mode == "fixture"
    assert settings.port == DEAD_TEST_PORT
    assert f":{FORBIDDEN_PORT}" not in settings.base_url


def test_the_default_test_port_has_no_listener() -> None:
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"{get_settings().base_url}/api/health", timeout=2.0)
    assert guard_violations == []  # a dead port is refused by the OS, not by the guard


def test_database_is_a_temp_file_never_the_dev_db(tmp_path: Path) -> None:
    url = get_settings().resolved_database_url
    assert url.startswith("sqlite:///")
    db_path = Path(url.removeprefix("sqlite:///"))
    assert db_path != DEV_DB and "data/sales0.db" not in url
    assert db_path.is_relative_to(tmp_path)


def test_an_explicit_port_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_PORT", "8799")
    reset_settings()
    assert get_settings().port == 8799


def test_socket_guard_refuses_port_8765_and_records_it() -> None:
    before = len(guard_violations)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(
            ConnectionRefusedError, match="tests must never connect to 8765"
        ):
            sock.connect(("127.0.0.1", FORBIDDEN_PORT))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        assert sock.connect_ex(("localhost", FORBIDDEN_PORT)) != 0
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"http://127.0.0.1:{FORBIDDEN_PORT}/api/health", timeout=2.0)
    hosts = [host for host, _ in guard_violations[before:]]
    assert len(hosts) == 3
    assert hosts[:2] == ["127.0.0.1", "localhost"]
    assert all(port == FORBIDDEN_PORT for _, port in guard_violations[before:])
    # Provoked on purpose: clear so the per-test check passes.
    del guard_violations[before:]


def test_socket_guard_refuses_ipv6_loopback_and_create_connection() -> None:
    before = len(guard_violations)
    if socket.has_ipv6:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            with pytest.raises(ConnectionRefusedError):
                sock.connect(("::1", FORBIDDEN_PORT, 0, 0))
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("localhost", FORBIDDEN_PORT), timeout=2.0)
    assert len(guard_violations) > before
    del guard_violations[before:]


def test_socket_guard_applies_to_other_threads() -> None:
    before = len(guard_violations)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.connect(("127.0.0.1", FORBIDDEN_PORT))
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(5)
    assert len(errors) == 1 and isinstance(errors[0], ConnectionRefusedError)
    assert guard_violations[before:] == [("127.0.0.1", FORBIDDEN_PORT)]
    del guard_violations[before:]


def test_a_swallowed_violation_still_fails_the_test() -> None:
    # Code under test that catches the refusal must not hide it: the per-test check fails.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect(("127.0.0.1", FORBIDDEN_PORT))
    except ConnectionRefusedError:
        pass
    assert guard_violations, "the swallowed attempt must still be recorded"
    with pytest.raises(pytest.fail.Exception, match="tests must never connect to 8765"):
        check_guard_violations()
    assert guard_violations == []  # the check clears what it reported
    check_guard_violations()  # and is a no-op on a clean list


def test_loopback_connects_to_other_ports_are_not_blocked_by_the_guard() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2.0):
            pass
    assert guard_violations == []


def test_safety_cooldowns_default_to_zero_and_channel_to_chromium() -> None:
    settings = get_settings()
    assert settings.safety_cooldown_min == 0
    assert settings.safety_cooldown_checkpoint_min == 0
    assert settings.linkedin_browser_channel == "chromium"


def test_browser_profile_dir_is_under_the_test_tmp_path(tmp_path: Path) -> None:
    from sales0.scraper import browser

    profile = browser.profile_dir(get_settings(), "chromium")
    assert Path(profile).is_relative_to(tmp_path)
    assert not Path(profile).is_relative_to(REPO_ROOT / "data")
