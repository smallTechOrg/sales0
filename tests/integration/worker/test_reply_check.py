"""q24: hosted replies - Cloud Run has no timer, so the laptop worker asks the hosted app to check Gmail every few
minutes; the Health page and the header say so."""

from __future__ import annotations

from fastapi.testclient import TestClient

from sales0.config import get_settings
from sales0.db.engine import session_scope
from sales0.services import replies
from sales0.worker.client import WorkerClient
from sales0.worker.runner import Worker


def test_the_worker_check_runs_and_the_health_says_who_checks(worker_client: WorkerClient, hosted: TestClient) -> None:
    with session_scope() as db:
        h = replies.poller_health(db)
    assert h.enabled and "worker" in h.label.lower() and "Cloud Run" in h.detail
    answer = worker_client.check_replies()
    assert answer.ran and answer.members == 0 and answer.errors == 0


def test_the_worker_loop_asks_on_its_own_interval(worker_client: WorkerClient, hosted: TestClient) -> None:
    w = Worker(get_settings(), worker_client)
    assert w.maybe_check_replies(now=1000.0) is True
    assert w.maybe_check_replies(now=1010.0) is False  # not due yet
    assert w.maybe_check_replies(now=1000.0 + 60 * get_settings().worker_reply_check_minutes) is True


def test_the_check_needs_the_token(hosted: TestClient) -> None:
    assert hosted.post("/api/worker/replies/check").status_code == 401
