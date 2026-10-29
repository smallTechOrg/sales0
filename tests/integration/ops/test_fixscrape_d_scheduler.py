"""Fixscrape D #14: the scheduled watch start is jittered per day; the seen_posts purge is wired into the scheduler."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from sales0.config import reset_settings
from sales0.db.engine import get_sessionmaker, session_scope
from sales0.db.models import ScheduledJob
from sales0.jobs import purge_job
from sales0.jobs.scheduler import Scheduler, WatchJob, purge_hooks

IST = timedelta(hours=5, minutes=30)


def ist(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 10, day, hh, mm, tzinfo=UTC) - IST


def next_run(clock_now: datetime) -> datetime:
    s = Scheduler(sessions=get_sessionmaker(), now=lambda: clock_now, rng=random.Random(1), jobs=[WatchJob()])
    s.set_enabled("profile_watch", True)
    with session_scope() as db:
        return db.scalar(select(ScheduledJob.next_run_at).where(ScheduledJob.name == "profile_watch"))


def test_watch_start_is_the_configured_time_plus_a_per_day_offset(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_WATCH_START_JITTER_MIN", "30")
    reset_settings()
    offsets = {}
    for day in (20, 21, 22, 23, 24, 25):
        at = next_run(ist(day - 1, 12))
        offset = (at.replace(tzinfo=UTC) if at.tzinfo is None else at) - ist(day, 5, 30)
        assert timedelta(0) <= offset <= timedelta(minutes=30)
        offsets[day] = offset
    assert len(set(offsets.values())) > 1  # not the same minute every day
    # deterministic per day: the same day gives the same slot again (restart-stable)
    again = next_run(ist(19, 12))
    assert (again.replace(tzinfo=UTC) if again.tzinfo is None else again) - ist(20, 5, 30) == offsets[20]


def test_zero_jitter_is_the_exact_time(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_WATCH_START_JITTER_MIN", "0")
    reset_settings()
    at = next_run(ist(19, 12))
    assert (at.replace(tzinfo=UTC) if at.tzinfo is None else at) == ist(20, 5, 30)


def test_a_jittered_slot_that_has_passed_rolls_to_tomorrow(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_WATCH_START_JITTER_MIN", "30")
    reset_settings()
    at = next_run(ist(20, 7))  # well after today's slot + jitter
    at = at.replace(tzinfo=UTC) if at.tzinfo is None else at
    assert ist(21, 5, 30) <= at <= ist(21, 6, 0)


def test_the_seen_posts_purge_hook_is_registered_with_the_scheduler(db_url: str) -> None:
    assert purge_job.HOOK_NAME in purge_hooks()
