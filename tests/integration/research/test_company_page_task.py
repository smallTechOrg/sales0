"""The laptop worker's company-page task end to end, in-process (hosted app + WorkerClient over TestClient).

A scripted page reader stands in for LinkedIn (the real reader is covered by the fixture-browser test); everything
else - capability negotiation, claim, safety gates, result upload, daily counters, cool-down - is production code."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sales0.clock import utcnow
from sales0.config import get_settings, reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import LinkedInAccount, ScrapeRun, WorkerTask
from sales0.services import linkedin_accounts, runs
from sales0.services.crm.actor import Actor
from sales0.services.research import linkedin_page
from sales0.worker import protocol as p
from sales0.worker.client import WorkerClient
from sales0.worker.runner import Worker
from tests.integration.jobs.helpers import FakeSearcher
from tests.integration.research.conftest import make_prospect
from tests.integration.services.helpers import safety, session_missing
from tests.integration.worker.conftest import BASE, TOKEN, hosted, hosted_env, llm  # noqa: F401 - fixtures

LI = "https://www.linkedin.com/company/acme-logistics/"
PAGE = SimpleNamespace(title="Acme Logistics | LinkedIn", url=LI, text="Acme Logistics\nCold-chain freight\n850 followers\nPosts: we opened Mumbai")


@pytest.fixture
def client(hosted: TestClient) -> Iterator[WorkerClient]:  # noqa: F811
    yield WorkerClient(BASE, TOKEN, http=hosted, sleep=lambda _s: None)


def make_worker(client: WorkerClient, reader, name: str = "laptop") -> Worker:  # noqa: ANN001
    calls: list[str] = []

    def read(searcher, slug: str):  # noqa: ANN001, ANN202
        calls.append(slug)
        if isinstance(reader, BaseException):
            raise reader
        return reader

    w = Worker(get_settings(), client, searcher_factory=lambda _s, _budget: FakeSearcher({}), page_reader=read, sleep=lambda _s: None)
    w.info = p.WorkerInfo(name=name, version="9.9.9", host="test")
    w.calls = calls  # type: ignore[attr-defined]
    return w


def queue(pid: int) -> int:
    with session_scope() as db:
        task, _ = linkedin_page.queue_for_prospect(db, pid, None, LI, actor=Actor("Ann"))
        assert task is not None
        return task.id


def task(tid: int) -> WorkerTask:
    with session_scope() as db:
        row = db.get(WorkerTask, tid)
        db.expunge(row)
        return row


def test_a_capable_worker_reads_one_page_and_the_text_is_stored(client: WorkerClient) -> None:
    pid = make_prospect(website=None)
    tid = queue(pid)
    w = make_worker(client, PAGE)
    assert w.poll_once() is True and w.calls == ["company/acme-logistics"]
    row = task(tid)
    assert (row.status, row.outcome, row.worker_name, row.page_loads) == ("done", "done", "laptop", 1)
    assert "we opened Mumbai" in row.result_text and row.result_url == LI
    with session_scope() as db:
        status = linkedin_page.status_for(db, pid, LI)
        assert status.state == "fetched" and status.fetched_at is not None
        assert runs.page_loads_today(db) == 1  # the same daily page-load counter as discovery / the watch
        assert linkedin_page.latest_text(db, pid).id == tid
    assert w.poll_once() is False  # nothing left: idle


def test_an_old_worker_never_gets_a_task_and_never_sees_the_task_key(hosted: TestClient) -> None:  # noqa: F811
    tid = queue(make_prospect(website=None))
    resp = hosted.post("/api/worker/claim", json={"worker": {"name": "old", "version": "0.9"}},
                       headers={"Authorization": f"Bearer {TOKEN}"})
    data = resp.json()["data"]
    assert resp.status_code == 200 and "task" not in data and data["reason"] == "nothing queued"
    assert task(tid).status == "queued"


def test_a_safety_stop_pauses_the_account_and_the_next_task_is_refused_for_the_cool_down(client: WorkerClient) -> None:
    pid = make_prospect(website=None)
    first = queue(pid)
    make_worker(client, safety("checkpoint")).poll_once()
    row = task(first)
    assert row.status == "failed" and row.outcome == "safety_stop" and "cool-down" in (row.error or "")
    with session_scope() as db:
        account = db.get(LinkedInAccount, linkedin_accounts.default_account_id(db))
        assert account.status == "needs_attention" and account.safety_state == "checkpoint"
        assert runs.safety_cooldown_until(db) is not None  # every later run AND task waits
    second = queue(pid)
    w = make_worker(client, PAGE)
    assert w.poll_once() is False and w.calls == []  # never opened the browser
    assert task(second).status == "failed" and task(second).outcome == "refused"


def test_daily_company_page_cap_and_one_browser_job_at_a_time(client: WorkerClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_COMPANY_PAGE_MAX_PER_DAY", "1")
    reset_settings()
    a, b = make_prospect(website=None), make_prospect(name="Ravi", company="Zen Corp", website=None)
    queue(a)
    w = make_worker(client, PAGE)
    assert w.poll_once() is True
    tb = queue(b)
    with session_scope() as db:  # a discovery run is in progress: the task waits (one browser at a time)
        run = ScrapeRun(trigger="manual", mode="live", status="running", started_at=utcnow(), worker_name="other", heartbeat_at=utcnow())
        db.add(run)
        db.flush()
        rid = run.id
    assert w.poll_once() is False and task(tb).status == "queued"
    with session_scope() as db:
        db.get(ScrapeRun, rid).status = "succeeded"
    assert w.poll_once() is False
    assert task(tb).status == "failed" and "company-page limit" in (task(tb).error or "")


def test_not_logged_in_and_page_errors_are_reported_without_retry(client: WorkerClient) -> None:
    from sales0.scraper.errors import ScrapeError

    pid = make_prospect(website=None)
    t1 = queue(pid)
    make_worker(client, session_missing()).poll_once()
    assert task(t1).status == "failed" and task(t1).outcome == "session_missing"
    with session_scope() as db:
        assert db.get(LinkedInAccount, linkedin_accounts.default_account_id(db)).status == "not_connected"
    t2 = queue(pid)
    make_worker(client, ScrapeError("LinkedIn returned HTTP 404")).poll_once()
    assert task(t2).status == "failed" and task(t2).outcome == "page_error" and "404" in task(t2).error


def test_a_claimed_task_whose_worker_vanished_is_failed_after_the_stale_window(client: WorkerClient) -> None:
    pid = make_prospect(website=None)
    tid = queue(pid)
    with session_scope() as db:
        assert linkedin_page.claim_next(db, "laptop") is not None
        db.get(WorkerTask, tid).claimed_at = utcnow() - timedelta(hours=1)
    with session_scope() as db:
        status = linkedin_page.status_for(db, pid, LI)
    assert status.state == "failed" and task(tid).outcome == "lost"


def test_a_repeated_result_upload_is_idempotent(hosted: TestClient) -> None:  # noqa: F811
    tid = queue(make_prospect(website=None))
    with session_scope() as db:
        linkedin_page.claim_next(db, "laptop")
    body = {"outcome": "done", "text": "Acme page", "url": LI, "page_loads": 1}
    h = {"Authorization": f"Bearer {TOKEN}"}
    assert hosted.post(f"/api/worker/tasks/{tid}/result", json=body, headers=h).json()["data"]["status"] == "done"
    assert hosted.post(f"/api/worker/tasks/{tid}/result", json={**body, "text": "other"}, headers=h).json()["data"]["status"] == "done"
    assert task(tid).result_text == "Acme page"
    assert hosted.post(f"/api/worker/tasks/{tid}/result", json=body).status_code == 401
