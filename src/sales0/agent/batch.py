"""Bounded-concurrency triage (agent.md -> Concurrency, Phase 1.4).

Workers only run ``run_triage`` (one LLM call each; no DB session). The caller's thread
consumes results as they complete and persists them. The first fatal LLM error
(``LLMAuthError`` / ``LLMModelNotFound``) stops further submissions.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

from sales0.agent.graph import run_triage
from sales0.agent.schemas import TriageDeps, TriageInput, TriageOutcome
from sales0.llm.errors import LLMAuthError, LLMModelNotFound

_FATAL = (LLMAuthError, LLMModelNotFound)


@dataclass(frozen=True)
class BatchItem:
    key: int
    inp: TriageInput


@dataclass(frozen=True)
class BatchResult:
    key: int
    outcome: TriageOutcome | None
    fatal: Exception | None = None


def triage_concurrently(
    items: Sequence[BatchItem],
    deps: TriageDeps,
    *,
    concurrency: int = 4,
    should_stop: Callable[[], bool] = lambda: False,
) -> Iterator[BatchResult]:
    """Yield one ``BatchResult`` per started item, in completion order.

    At most ``concurrency`` calls are in flight. ``should_stop`` is polled before each
    submission. After a fatal error nothing further is submitted; already running calls
    still finish (and are yielded) so their cost is never lost. Items never started yield
    nothing: the caller leaves them pending. Non-fatal problems come back as a
    ``failed`` outcome from ``run_triage``; an unexpected exception is re-raised.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    pending = iter(items)
    in_flight: dict[Future[TriageOutcome], int] = {}
    stopped = False
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="triage") as pool:
        try:
            while True:
                while not stopped and len(in_flight) < concurrency:
                    if should_stop():
                        stopped = True
                        break
                    item = next(pending, None)
                    if item is None:
                        stopped = True
                        break
                    in_flight[pool.submit(run_triage, item.inp, deps)] = item.key
                if not in_flight:
                    return
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for fut in done:
                    key = in_flight.pop(fut)
                    exc = fut.exception()
                    if exc is None:
                        yield BatchResult(key=key, outcome=fut.result())
                    elif isinstance(exc, _FATAL):
                        stopped = True
                        yield BatchResult(key=key, outcome=None, fatal=exc)
                    else:
                        raise exc
        finally:
            for fut in in_flight:
                fut.cancel()
