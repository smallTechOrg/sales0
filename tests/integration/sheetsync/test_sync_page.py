"""The Sync page, its JSON status, the scheduler job and the CLI against the CRM (fake source, no network)."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sales0.db.engine import session_scope
from sales0.db.models import Enquiry
from sales0.db.models.sheet_sync import SheetSyncState
from sales0.jobs import sheet_sync_job
from sales0.services.sheetsync import StaticSource, TabSpec
from sales0.web.app import create_app
from tests.integration.sheetsync.conftest import table_counts
from tests.unit.sheetsync import fixtures as fx

HX = {"HX-Request": "true"}


class GatedSource(StaticSource):
    """A source that blocks until released, to hold a sync 'running'."""

    def __init__(self, texts: dict[str, str]) -> None:
        super().__init__(texts)
        self.entered = threading.Event()
        self.release = threading.Event()

    def fetch(self, tab: TabSpec) -> str:
        self.entered.set()
        assert self.release.wait(10), "test never released the sync"
        return super().fetch(tab)


@pytest.fixture
def source() -> Iterator[StaticSource]:
    src = StaticSource(fx.all_tabs_csv())
    sheet_sync_job.set_source_factory(lambda: src)
    yield src
    sheet_sync_job.set_source_factory(None)


@pytest.fixture
def client(db_url: str, source: StaticSource) -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def _status(client: TestClient) -> dict:
    return client.get("/api/sync/status", headers={"Accept": "application/json"}).json()["data"]


def wait_idle(client: TestClient, timeout: float = 15.0, *, after: object = None) -> dict:
    """Wait until the sync is idle AND has finished a run newer than ``after`` (the ``last_finished_at`` seen before
    the click). Idle alone is not enough: the job thread may not have started yet, so ``running`` is still false."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        data = _status(client)
        if not data["running"] and data.get("last_finished_at") != after and data.get("last_status") is not None:
            return data
        time.sleep(0.05)
    raise AssertionError("sync never finished")


def test_page_before_any_sync(client: TestClient) -> None:
    html = client.get("/sync").text
    assert "Google Sheet sync" in html and "never writes to your sheets" in html
    assert 'id="sync-now"' in html and "No sync has run yet." in html
    assert 'id="sync-tabs"' not in html


def test_sync_now_runs_the_job_and_shows_per_tab_counts(client: TestClient) -> None:
    before = _status(client).get("last_finished_at")
    r = client.post("/sync/run", headers=HX)
    assert r.status_code == 200 and 'id="sync-status"' in r.text and "Sync started." in r.text
    status = wait_idle(client, after=before)
    assert status["last_status"] == "ok" and status["last_error"] is None
    tabs = {t["tab"]: t for t in status["tabs"]}
    assert tabs["Active Enquiries"]["added"] == 7 and tabs["Active Enquiries"]["errors"] == 1
    assert set(tabs) == {t.name for t in fx.TABS}
    html = client.get("/sync").text
    assert 'id="sync-tabs"' in html and "Active Enquiries" in html and "(ok)" not in html and "ok</span>" in html
    for secret in ("acme", "@", "linkedin.com", "asha"):
        assert secret not in html.lower().split("<main")[-1], secret
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(Enquiry)) == 9
        row = db.get(SheetSyncState, 1)
        assert row is not None and row.runs_total == 1 and row.last_dry_run is False
    # a second run is a no-op and the state survives (persisted)
    before = _status(client).get("last_finished_at")
    client.post("/sync/run", headers=HX)
    again = wait_idle(client, after=before)
    assert sum(t["added"] + t["updated"] + t["removed"] for t in again["tabs"]) == 0


def test_a_second_click_while_running_is_refused(client: TestClient, db_url: str) -> None:
    gated = GatedSource(fx.all_tabs_csv())
    sheet_sync_job.set_source_factory(lambda: gated)
    assert client.post("/sync/run", headers=HX).status_code == 200
    assert gated.entered.wait(10)
    page = client.get("/sync/status").text
    assert 'data-state="running"' in page and 'hx-trigger="every 2s"' in page and "disabled" in page
    busy = client.post("/sync/run", headers=HX)
    assert busy.status_code == 200 and "A sync is already running." in busy.text and "toast--error" in busy.text
    plain = client.post("/sync/run", headers={"Accept": "application/json"})
    assert plain.status_code == 409 and plain.json()["error"]["code"] == "sync_running"
    gated.release.set()
    assert wait_idle(client)["last_status"] == "ok"
    assert 'hx-trigger="every 2s"' not in client.get("/sync/status").text


def test_dry_run_previews_the_diff_and_writes_nothing(client: TestClient) -> None:
    r = client.post("/sync/dry-run", headers=HX)
    assert r.status_code == 200 and "Preview ready" in r.text
    html = client.get("/sync").text
    assert 'id="sync-dryrun"' in html and "nothing was written" in html and "Would add" in html
    assert table_counts()["enquiries"] == 0 and table_counts()["sheet_sources"] == 0
    with session_scope() as db:
        assert db.get(SheetSyncState, 1) is None  # a dry run does not even touch sheet_sync_state
    assert "No sync has run yet." in html


def test_one_click_enable_flips_the_scheduler_job(client: TestClient) -> None:
    html = client.get("/sync").text
    assert 'id="sync-auto-on"' in html
    r = client.post("/sync/enable", headers=HX)
    assert "Automatic sync enabled." in r.text and 'id="sync-auto-off"' in r.text
    sched = client.get("/schedule").text
    assert 'id="job-sheet_sync" data-enabled="1"' in sched
    assert "Waiting for the Phase 2" not in sched
    client.post("/sync/disable", headers=HX)
    assert 'id="job-sheet_sync" data-enabled="0"' in client.get("/schedule").text


def test_the_scheduler_job_is_ready_and_runs_now(client: TestClient) -> None:
    from sales0.jobs.scheduler import get_scheduler

    job = get_scheduler().defs["sheet_sync"]
    assert job.available() and job.note(get_scheduler().ctx) is None
    result = get_scheduler().run_now("sheet_sync")
    assert result.status == "ok"
    assert wait_idle(client)["last_status"] == "ok"


def test_api_status_shape(client: TestClient) -> None:
    data = client.get("/api/sync/status").json()["data"]
    assert set(data) >= {"enabled", "interval_min", "running", "next_run_at", "last_started_at", "last_finished_at", "last_status", "last_error", "tabs"}
    assert data["tabs"] == [] and data["last_status"] is None


def test_a_failing_tab_shows_partial_with_its_problem(client: TestClient, source: StaticSource) -> None:
    source.texts["Groups"] = RuntimeError("down")
    client.post("/sync/run", headers=HX)
    status = wait_idle(client)
    assert status["last_status"] == "partial"
    html = client.get("/sync").text
    assert "fetch error: fetch-failed:RuntimeError" in html and "could not be read" in html
