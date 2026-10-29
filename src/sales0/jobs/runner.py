"""Single-flight background job runner.

Only one browser job (connect / check / run) may run at a time, because the persistent
Chromium profile cannot be opened twice. ``submit`` refuses a second job with
``DomainError(job_busy)``. The job runs on a daemon thread that opens its own DB
sessions (never a request's).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Literal

from sales0.errors import DomainError
from sales0.logging import bind_context, clear_context, get_logger

log = get_logger("sales0.jobs")

JobName = Literal["connect", "check", "run"]

_ACTION_LABELS: dict[str, str] = {"connect": "Connect", "check": "Re-check", "run": "a run"}


def job_busy_error(action: str) -> DomainError:
    label = _ACTION_LABELS.get(action, action)
    return DomainError("job_busy", f"LinkedIn is busy ({label} in progress) — wait for it to finish.", 409)


class JobRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._name: str | None = None
        self._thread: threading.Thread | None = None
        self._started_monotonic: float | None = None
        self._cancel = threading.Event()
        self._connect_elapsed_s: int | None = None

    # ------------------------------------------------------------------ contract
    def submit(self, name: JobName, fn: Callable[[], None]) -> None:
        from sales0.services._common import require_local_browser

        require_local_browser("A browser job")  # backstop: hosted mode never starts one
        with self._lock:
            if self._name is not None:
                raise job_busy_error(self._name)
            self._name = name
            self._started_monotonic = time.monotonic()
            self._cancel.clear()
            self._connect_elapsed_s = 0 if name == "connect" else None
            thread = threading.Thread(target=self._run, args=(name, fn), name=f"sales0-job-{name}", daemon=True)
            self._thread = thread
        thread.start()

    def busy(self) -> str | None:
        with self._lock:
            return self._name

    def cancel_connect(self) -> None:
        with self._lock:
            if self._name == "connect":
                self._cancel.set()

    # ------------------------------------------------------------------ helpers for jobs
    def connect_cancelled(self) -> bool:
        return self._cancel.is_set()

    def set_connect_elapsed(self, seconds: int) -> None:
        with self._lock:
            if self._name == "connect":
                self._connect_elapsed_s = int(seconds)

    def connect_elapsed_s(self) -> int | None:
        with self._lock:
            if self._name != "connect":
                return None
            if self._connect_elapsed_s:
                return self._connect_elapsed_s
            if self._started_monotonic is None:
                return 0
            return int(time.monotonic() - self._started_monotonic)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the current job finishes. Returns True if idle afterwards."""
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.busy() is None

    def shutdown(self, timeout: float = 60.0) -> None:
        self.cancel_connect()
        self.wait(timeout)

    # ------------------------------------------------------------------ internals
    def _run(self, name: str, fn: Callable[[], None]) -> None:
        clear_context()
        bind_context(job=name)
        try:
            fn()
        except Exception:  # noqa: BLE001 — a job crash must never kill the runner
            log.exception("job_crashed", job=name)
        finally:
            clear_context()
            with self._lock:
                self._name = None
                self._started_monotonic = None
                self._connect_elapsed_s = None
                self._cancel.clear()


_runner_lock = threading.Lock()
_runner: JobRunner | None = None


def get_runner() -> JobRunner:
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = JobRunner()
        return _runner


def reset_runner() -> None:
    """Test seam: let any in-flight job finish, then drop the singleton."""
    global _runner
    with _runner_lock:
        runner = _runner
        _runner = None
    if runner is not None:
        runner.shutdown()


__all__ = ["JobName", "JobRunner", "get_runner", "job_busy_error", "reset_runner"]
