"""triage_concurrently: bounded pool, completion order, fatal stop, stop flag, cost intact."""

from __future__ import annotations

import threading
import time

import pytest

from sales0.agent.batch import BatchItem, triage_concurrently
from sales0.agent.schemas import TriageInput
from sales0.llm.errors import LLMAuthError, LLMModelNotFound
from tests.support.fake_llm import FakeLLMClient, triage_answer


def _items(n: int) -> list[BatchItem]:
    return [
        BatchItem(key=i, inp=TriageInput(enquiry_id=i, subject="seen_post", text=f"post number {i}"))
        for i in range(n)
    ]


def test_all_items_yield_once_with_cost_and_seen_post_context(make_deps) -> None:
    fake = FakeLLMClient(responder=lambda req: triage_answer())
    results = list(triage_concurrently(_items(10), make_deps(fake), concurrency=4))
    assert sorted(r.key for r in results) == list(range(10))
    assert all(r.fatal is None and r.outcome and r.outcome.status == "done" for r in results)
    assert all(r.outcome.llm_responses[0].cost_usd_micros > 0 for r in results if r.outcome)
    assert fake.calls == 10
    assert all("seen_post_id" in q.log_context and "enquiry_id" not in q.log_context for q in fake.requests)


def test_concurrency_is_bounded_and_actually_parallel(make_deps) -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    def slow(req):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return triage_answer()

    list(triage_concurrently(_items(12), make_deps(FakeLLMClient(responder=slow)), concurrency=4))
    assert 2 <= peak <= 4


def test_concurrency_one_is_serial(make_deps) -> None:
    peak = active = 0
    lock = threading.Lock()

    def slow(req):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return triage_answer()

    list(triage_concurrently(_items(5), make_deps(FakeLLMClient(responder=slow)), concurrency=1))
    assert peak == 1


def test_empty_batch_and_invalid_concurrency(make_deps) -> None:
    fake = FakeLLMClient(responder=lambda req: triage_answer())
    assert list(triage_concurrently([], make_deps(fake))) == []
    assert fake.calls == 0
    with pytest.raises(ValueError):
        list(triage_concurrently(_items(1), make_deps(fake), concurrency=0))


@pytest.mark.parametrize("exc", [LLMAuthError("bad key"), LLMModelNotFound("no model")])
def test_fatal_error_stops_further_submissions(make_deps, exc) -> None:
    def respond(req):
        raise exc

    fake = FakeLLMClient(responder=respond)
    results = list(triage_concurrently(_items(20), make_deps(fake), concurrency=2))
    fatals = [r for r in results if r.fatal is not None]
    assert fatals and isinstance(fatals[0].fatal, type(exc)) and fatals[0].outcome is None
    assert fake.calls < 20  # unstarted items were never submitted


def test_should_stop_prevents_new_submissions_but_finishes_running_ones(make_deps) -> None:
    fake = FakeLLMClient(responder=lambda req: triage_answer())
    results = []
    stop = False

    def should_stop() -> bool:
        return stop

    for r in triage_concurrently(_items(10), make_deps(fake), concurrency=1, should_stop=should_stop):
        results.append(r)
        stop = True
    assert len(results) == 1 and fake.calls == 1


def test_oracle_fake_answers_by_text_and_counts_calls(make_deps) -> None:
    fake = FakeLLMClient.with_oracle(lambda t: "genuine" if "partner" in t else "discard")
    items = [
        BatchItem(1, TriageInput(enquiry_id=1, text="need a training partner <b>now</b>")),
        BatchItem(2, TriageInput(enquiry_id=2, text="register for our webinar")),
    ]
    got = {r.key: r.outcome.result.verdict for r in triage_concurrently(items, make_deps(fake)) if r.outcome and r.outcome.result}
    assert got == {1: "genuine", 2: "discard"} and fake.calls == 2
