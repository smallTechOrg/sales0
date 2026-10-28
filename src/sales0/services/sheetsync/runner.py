"""Interval scheduler wrapper, pure Python with injectable clock/executor/rng.

``tick()`` is the only scheduling primitive: call it periodically (``start()`` spawns a
daemon thread that does so). It never overlaps itself, survives job errors, jitters the
interval, and exposes :meth:`status` for the Setup/Sync page.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sales0.services.sheetsync.report import SyncReport

DEFAULT_INTERVAL_MIN = 30
FIRST_RUN_DELAY_S = 20.0


class SyncState(Protocol):
    """Persistence for survive-restart behaviour (see ``state.py``)."""

    def load_last_finished(self) -> datetime | None: ...

    def save_run(self, started_at: datetime, report: SyncReport | None, error: str | None) -> None: ...


@dataclass
class RunnerStatus:
    enabled: bool
    interval_min: int
    running: bool
    runs: int = 0
    consecutive_failures: int = 0
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_ok: bool | None = None
    last_error: str | None = None
    last_report: dict[str, Any] | None = None
    next_run_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class SheetSyncRunner:
    def __init__(
        self,
        job: Callable[[], SyncReport],
        *,
        interval_min: int = DEFAULT_INTERVAL_MIN,
        jitter: float = 0.1,
        first_run_delay_s: float = FIRST_RUN_DELAY_S,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        executor: Executor | None = None,
        rng: random.Random | None = None,
        state: SyncState | None = None,
    ) -> None:
        self._job = job
        self._interval_min = max(0, int(interval_min))
        self._jitter = max(0.0, min(jitter, 0.5))
        self._first_delay = first_run_delay_s
        self._now = now
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="sheet-sync")
        self._rng = rng or random.Random()
        self._state = state
        self._lock = threading.Lock()
        self._running = False
        self._status = RunnerStatus(enabled=self._interval_min > 0, interval_min=self._interval_min, running=False)
        self._next: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._arm()

    # ------------------------------------------------------------------ scheduling
    def _jittered(self, base: timedelta) -> timedelta:
        return base * (1 + self._rng.uniform(-self._jitter, self._jitter))

    def _arm(self) -> None:
        """At construction (= boot) pick the first due time: soon after boot, or last finish + interval after a restart."""
        if self._interval_min <= 0:
            return
        now = self._now()
        first = now + timedelta(seconds=self._first_delay * (1 + self._rng.random() * self._jitter))
        last = self._state.load_last_finished() if self._state else None
        if last is not None:
            due = last + self._jittered(timedelta(minutes=self._interval_min))
            first = max(first, due)
        self._next = first
        self._status.next_run_at = first

    def tick(self) -> bool:
        """Start a run if one is due and none is active. Returns True when a run was submitted."""
        if self._interval_min <= 0 or self._next is None or self._now() < self._next:
            return False
        return self._submit()

    def trigger_now(self) -> bool:
        """Manual 'Sync now'. False if a run is already active. Works even when disabled."""
        return self._submit()

    def _submit(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._status.running = True
        # Push the next due time out immediately so a slow run cannot trigger a second one.
        if self._interval_min > 0:
            self._next = self._now() + self._jittered(timedelta(minutes=self._interval_min))
            self._status.next_run_at = self._next
        self._executor.submit(self._execute)
        return True

    def _execute(self) -> None:
        started = self._now()
        report: SyncReport | None = None
        error: str | None = None
        try:
            report = self._job()
        except Exception as exc:  # noqa: BLE001 — the scheduler must survive any job failure
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
        finished = self._now()
        with self._lock:
            st = self._status
            st.runs += 1
            st.last_started_at, st.last_finished_at = started, finished
            st.last_ok = error is None and (report is None or report.ok)
            st.last_error = error or (None if report is None or report.ok else "one or more tabs failed")
            st.last_report = report.to_dict() if report else None
            st.consecutive_failures = 0 if st.last_ok else st.consecutive_failures + 1
            self._running = False
            st.running = False
        if self._state is not None:
            try:
                self._state.save_run(started, report, error)
            except Exception:  # noqa: BLE001 — bookkeeping must not kill the loop
                pass

    # ------------------------------------------------------------------ lifecycle
    def status(self) -> RunnerStatus:
        with self._lock:
            return RunnerStatus(**{**self._status.__dict__})

    def start(self, poll_s: float = 1.0) -> None:
        if self._thread is not None or self._interval_min <= 0:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(poll_s)

        self._thread = threading.Thread(target=loop, name="sheet-sync-scheduler", daemon=True)
        self._thread.start()

    def stop(self, wait_s: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(wait_s)
            self._thread = None
        deadline = time.monotonic() + wait_s
        while self._running and time.monotonic() < deadline:
            time.sleep(0.02)
        if isinstance(self._executor, ThreadPoolExecutor):
            self._executor.shutdown(wait=False)


class InlineExecutor(Executor):
    """Runs submitted work immediately on the caller's thread (deterministic tests)."""

    def submit(self, fn, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        from concurrent.futures import Future

        fut: Future[Any] = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001
            fut.set_exception(exc)
        return fut
