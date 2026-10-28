"""Fixscrape D #5: one sales0 process per data directory (flock on <data_dir>/.sales0.lock)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sales0.config import get_settings, reset_settings
from sales0.datalock import LOCK_NAME, DataDirLocked
from sales0.web.app import create_app

HOLDER = (
    "import sys\n"
    "from pathlib import Path\n"
    "from sales0.datalock import DataDirLock\n"
    "lock = DataDirLock(Path(sys.argv[1])); lock.acquire()\n"
    "print('held', flush=True)\n"
    "sys.stdin.read()\n"
)


def _hold(data_dir: Path) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(data_dir)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
    return proc


def _release(proc: subprocess.Popen[str]) -> None:
    assert proc.stdin is not None
    proc.stdin.close()
    proc.wait(10)


def test_boot_is_refused_while_another_process_holds_the_data_dir(db_url: str) -> None:
    data_dir = get_settings().data_path
    proc = _hold(data_dir)
    try:
        with pytest.raises(DataDirLocked) as info:
            with TestClient(create_app()):
                pass
        msg = str(info.value)
        assert "Another sales0 process" in msg and "ONE sales0" in msg and "Stop the other process" in msg
    finally:
        _release(proc)
    # the holder is gone (the kernel dropped its lock): the same dir boots again
    with TestClient(create_app()) as c:
        assert c.get("/api/health").status_code == 200


def test_lock_is_released_on_shutdown_so_a_second_app_on_the_same_dir_boots(db_url: str) -> None:
    for _ in range(2):
        with TestClient(create_app()) as c:
            assert (get_settings().data_path / LOCK_NAME).exists()
            assert c.get("/api/health").status_code == 200


def test_apps_of_one_process_share_the_lock_and_the_last_one_releases_it(db_url: str) -> None:
    """Existing suites open several clients on one data dir; only OTHER processes are refused."""
    data_dir = get_settings().data_path
    with TestClient(create_app()), TestClient(create_app()):
        pass
    proc = _hold(data_dir)  # both released: a foreign holder can take it, and then boot is refused again
    try:
        with pytest.raises(DataDirLocked):
            with TestClient(create_app()):
                pass
    finally:
        _release(proc)
    first = TestClient(create_app())
    first.__enter__()
    second = TestClient(create_app())
    second.__enter__()
    first.__exit__(None, None, None)
    foreign = subprocess.run(  # still held by the second app
        [sys.executable, "-c", HOLDER.replace("print('held', flush=True)", "print('got it', flush=True)"), str(data_dir)],
        input="", capture_output=True, text=True,
    )
    assert foreign.returncode != 0 and "Another sales0 process" in foreign.stderr
    second.__exit__(None, None, None)


def test_different_data_dirs_do_not_conflict(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = get_settings()
    with TestClient(create_app(first)):
        other = first.model_copy(update={"data_dir": str(tmp_path / "other-data")})
        # the lock is per directory; the second app only needs its own (empty) data dir for the lock
        from sales0.datalock import DataDirLock

        lock = DataDirLock(other.data_path)
        lock.acquire()
        lock.release()


def test_escape_hatch_skips_the_lock(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _hold(get_settings().data_path)
    try:
        monkeypatch.setenv("SALES0_ALLOW_MULTI_PROCESS", "1")
        reset_settings()
        with TestClient(create_app()) as c:
            assert c.get("/api/health").status_code == 200
    finally:
        _release(proc)


def test_lock_is_taken_before_interrupted_runs_are_recovered(db_url: str) -> None:
    """The refused boot must not touch the live process's runs (recover_interrupted aborts them)."""
    from sales0.clock import utcnow
    from sales0.db.engine import session_scope
    from sales0.db.models import ScrapeRun

    with session_scope() as db:
        run = ScrapeRun(trigger="manual", mode="live", status="running", started_at=utcnow())
        db.add(run)
        db.flush()
        rid = run.id
    proc = _hold(get_settings().data_path)
    try:
        with pytest.raises(DataDirLocked):
            with TestClient(create_app()):
                pass
    finally:
        _release(proc)
    with session_scope() as db:
        assert db.get(ScrapeRun, rid).status == "running"
