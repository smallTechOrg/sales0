"""The single-flight job runner: one browser job at a time, crash-safe, cancellable connect."""

from __future__ import annotations

import threading

import pytest

from sales0.errors import DomainError
from sales0.jobs.runner import JobRunner, get_runner, reset_runner


def test_submit_runs_the_job_and_clears_busy_afterwards() -> None:
    runner = JobRunner()
    ran = threading.Event()
    runner.submit("check", ran.set)
    assert runner.wait(5)
    assert ran.is_set()
    assert runner.busy() is None


def test_second_submit_while_busy_is_refused_with_job_busy() -> None:
    runner = JobRunner()
    release = threading.Event()
    runner.submit("run", lambda: release.wait(5))
    try:
        assert runner.busy() == "run"
        with pytest.raises(DomainError) as info:
            runner.submit("connect", lambda: None)
        assert info.value.code == "job_busy"
        assert info.value.http_status == 409
        assert "LinkedIn is busy" in info.value.message and "run" in info.value.message
    finally:
        release.set()
        runner.wait(5)
    assert runner.busy() is None


def test_a_crashing_job_does_not_wedge_the_runner() -> None:
    runner = JobRunner()

    def boom() -> None:
        raise RuntimeError("boom")

    runner.submit("check", boom)
    assert runner.wait(5)
    ran = threading.Event()
    runner.submit("check", ran.set)
    runner.wait(5)
    assert ran.is_set()


def test_cancel_connect_signals_only_a_running_connect_job() -> None:
    runner = JobRunner()
    seen: list[bool] = []
    started = threading.Event()

    def connect() -> None:
        started.set()
        for _ in range(500):
            if runner.connect_cancelled():
                seen.append(True)
                return
            threading.Event().wait(0.01)
        seen.append(False)

    runner.cancel_connect()  # idle: no effect, nothing to cancel
    runner.submit("connect", connect)
    started.wait(5)
    runner.set_connect_elapsed(7)
    assert runner.connect_elapsed_s() == 7
    runner.cancel_connect()
    runner.wait(10)
    assert seen == [True]
    assert runner.connect_elapsed_s() is None


def test_get_runner_is_a_singleton_until_reset() -> None:
    first = get_runner()
    assert get_runner() is first
    reset_runner()
    assert get_runner() is not first
