"""Background reply poller: one daemon thread, one cycle at a time (never overlapping).

``ReplyPoller.run_cycle()`` is also the manual "Check now" entry point and what tests call.
Interval is ``SALES0_REPLY_POLL_MINUTES`` (0 = the thread is never started). Read-only on Gmail.
"""

from __future__ import annotations

import threading

from sales0.config import Settings, get_settings
from sales0.llm import LLMClient
from sales0.logging import get_logger
from sales0.services import replies

log = get_logger("sales0.jobs.reply_poller")


class ReplyPoller:
    def __init__(self, settings: Settings | None = None, llm: LLMClient | None = None) -> None:
        self._settings = settings or get_settings()
        self._llm = llm
        self._cycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self._settings.reply_poll_minutes > 0

    def running(self) -> bool:
        return self._cycle_lock.locked()

    def run_cycle(self, *, force: bool = False) -> list[replies.PollResult] | None:
        """One cycle; returns None (and does nothing) if a cycle is already in flight."""
        if not self._cycle_lock.acquire(blocking=False):
            return None
        try:
            budget = self._settings.hosted_action_budget_s if self._settings.is_hosted else None
            return replies.poll_all(llm=self._llm, settings=self._settings, force=force, budget_s=budget)
        except Exception:  # noqa: BLE001 - the loop must survive anything
            log.exception("reply_cycle_crashed")
            return []
        finally:
            self._cycle_lock.release()

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sales0-reply-poller", daemon=True)
        self._thread.start()
        log.info("reply_poller_started", minutes=self._settings.reply_poll_minutes)

    def _loop(self) -> None:
        interval = self._settings.reply_poll_minutes * 60
        # First cycle shortly after boot, then every interval; Event.wait makes stop() prompt.
        if self._stop.wait(5):
            return
        while not self._stop.is_set():
            self.run_cycle()
            if self._stop.wait(interval):
                return

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)


_poller: ReplyPoller | None = None
_lock = threading.Lock()


def get_poller() -> ReplyPoller:
    global _poller
    with _lock:
        if _poller is None:
            _poller = ReplyPoller()
        return _poller


def reset_poller() -> None:
    """Test seam: stop and drop the singleton."""
    global _poller
    with _lock:
        poller, _poller = _poller, None
    if poller is not None:
        poller.stop(5)


__all__ = ["ReplyPoller", "get_poller", "reset_poller"]
