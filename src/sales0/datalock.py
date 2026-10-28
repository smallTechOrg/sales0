"""One sales0 process per data directory (exclusive, non-blocking ``flock`` on ``<data_dir>/.sales0.lock``).

The job runner's lock, the daily caps and ``recover_interrupted_runs_on_startup`` (which aborts every
queued/running run) all assume a single process owns the database. A second process on the same data
directory would abort the first one's live run and could double the LinkedIn page loads, so the app refuses
to boot instead. The kernel drops the lock when the process dies, so a crash never leaves a stale lock.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

try:  # fcntl is POSIX only; elsewhere the guard is a no-op (sales0 targets macOS / Linux)
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

LOCK_NAME = ".sales0.lock"


class DataDirLocked(RuntimeError):
    """Another sales0 process is already using this data directory."""


def lock_message(data_dir: Path) -> str:
    return (
        f"Another sales0 process is already using the data directory {data_dir}. "
        "Run only ONE sales0 per data directory (two would abort each other's runs and double the "
        "LinkedIn page loads on the same account). Stop the other process first, or point this one at a "
        "different SALES0_DATA_DIR. (Override at your own risk: SALES0_ALLOW_MULTI_PROCESS=1.)"
    )


# Locks this process holds: resolved dir -> [fd, holders]. The lock guards against OTHER processes; several
# apps of THIS process on one data dir (a test opening two TestClients on one app database, or a reload)
# share the one flock, which is dropped when the last of them shuts down.
_held: dict[str, list[int]] = {}
_guard = threading.Lock()


class DataDirLock:
    """Held from ``acquire()`` to ``release()``; ``acquire`` raises ``DataDirLocked`` when ANOTHER process holds it."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / LOCK_NAME
        self._key: str | None = None

    def acquire(self) -> None:
        if fcntl is None or self._key is not None:  # pragma: no cover
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        key = str(self.path.resolve())
        with _guard:
            held = _held.get(key)
            if held is not None:
                held[1] += 1
                self._key = key
                return
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                raise DataDirLocked(lock_message(self.data_dir)) from exc
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            _held[key] = [fd, 1]
            self._key = key

    def release(self) -> None:
        key, self._key = self._key, None
        if key is None or fcntl is None:
            return
        with _guard:
            held = _held.get(key)
            if held is None:
                return
            held[1] -= 1
            if held[1] > 0:
                return
            del _held[key]
            try:
                fcntl.flock(held[0], fcntl.LOCK_UN)
            finally:
                os.close(held[0])


__all__ = ["LOCK_NAME", "DataDirLock", "DataDirLocked", "lock_message"]
