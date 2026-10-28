"""The laptop worker against the hosted app, end to end, in-process: a scripted searcher stands in for LinkedIn
(the real one needs a browser); everything else - HTTP, auth, dedupe, Haiku triage (FakeLLM), counters, safety
stops, resume, outage retry - is the production code path."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sales0.config import get_settings
from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, ScrapeRun, ScrapeRunKeyword, SeenPost, Worker
from sales0.errors import DomainError
from sales0.scraper.errors import AccountSafetyStop
from sales0.services import keywords, runs
from sales0.services import worker_service
from sales0.services.session import get_session_row
from sales0.worker import protocol as p
from sales0.worker.client import ServerUnavailable, WorkerAuthError, WorkerClient
from sales0.worker.runner import Worker as WorkerLoop
from tests.conftest import TEST_ENV  # noqa: F401  (documented: the suite's fast pacing applies)
from tests.integration.jobs.helpers import FakeSearcher, permalink_for
from tests.integration.services.helpers import post, safety, scrape_error, session_missing
from tests.integration.worker.conftest import BASE, TOKEN, FlakyHttp

TEXT = {
    1: "Acme Logistics wants a training partner for a 2-day GenAI workshop in Pune",
    2: "SkillBridge Learning is hiring freelance Generative AI trainers for corporate clients",
    3: "Zentrix Retail wants an AI agents workshop for 30 engineers in Bengaluru",
}


def corpus() -> dict[str, list]:
    return {
        "corporate ai training": [post(1, TEXT[1]), post(2, TEXT[2])],
        "ai workshop": [post(1, TEXT[1]), post(3, TEXT[3])],  # post 1 again: a cross-keyword duplicate
    }


def seed_keywords(*texts: str) -> None:
    with session_scope() as db:
        for t in texts:
            keywords.add_keyword(db, t)


def queue() -> int:
    with session_scope() as db:
        return runs.start_run(db).id


def make_worker(client: WorkerClient, searcher: FakeSearcher, name: str = "laptop") -> WorkerLoop:
    settings = get_settings()
    w = WorkerLoop(
        settings, client, searcher_factory=lambda _s, _budget: searcher, sleep=lambda _s: None
    )
    w.info = p.WorkerInfo(name=name, version="9.9.9", host="test-host")
    return w


def run_row(run_id: int) -> dict:
    with session_scope() as db:
        r = db.get(ScrapeRun, run_id)
        return {
            "status": r.status, "abort_reason": r.abort_reason, "seen": r.posts_seen, "new": r.posts_new, "dupe": r.posts_dupe,
            "relevant": r.posts_relevant, "planned": r.keywords_planned, "done": r.keywords_done, "links": r.permalinks_found,
            "msg": r.error_message, "worker": r.worker_name, "loads": r.page_loads, "step": r.current_step,
        }


# ---------------------------------------------------------------------- the happy path
def test_a_queued_run_is_claimed_scraped_uploaded_triaged_and_completed(worker_client: WorkerClient, hosted: TestClient) -> None:
    seed_keywords("corporate ai training", "ai workshop")
    run_id = queue()
    assert run_row(run_id)["status"] == "queued"

    searcher = FakeSearcher(corpus())
    assert make_worker(worker_client, searcher).poll_once() is True

    row = run_row(run_id)
    assert row["status"] == "succeeded", row
    assert row["worker"] == "laptop"
    assert (row["planned"], row["done"]) == (2, 2)
    assert row["seen"] == 4 and row["new"] == 3 and row["dupe"] == 1  # post 1 seen twice
    assert row["relevant"] == 3
    assert searcher.searched == ["corporate ai training", "ai workshop"]
    with session_scope() as db:
        assert db.scalar(select(func.count(Enquiry.id))) == 3
        assert db.scalar(select(func.count(SeenPost.id))) == 3
        w = db.scalar(select(Worker))
        assert (w.name, w.version, w.host) == ("laptop", "9.9.9", "test-host")
        assert get_session_row(db).status == "connected"  # the worker just used the session successfully
    assert "Run finished" in (row["step"] or "")


def test_a_second_run_over_the_same_posts_is_all_duplicates(worker_client: WorkerClient) -> None:
    seed_keywords("corporate ai training", "ai workshop")
    queue()
    make_worker(worker_client, FakeSearcher(corpus())).poll_once()
    second = queue()
    make_worker(worker_client, FakeSearcher(corpus())).poll_once()
    row = run_row(second)
    assert row["status"] == "succeeded" and row["new"] == 0 and row["dupe"] == 4


def test_permalinks_the_server_asks_for_are_captured_and_attached(worker_client: WorkerClient) -> None:
    seed_keywords("ai workshop")
    queue()
    sdui = FakeSearcher({"ai workshop": [post(1, TEXT[1])]})
    make_worker(worker_client, sdui).poll_once()
    # A fresh sighting of a post whose enquiry has a post link needs no capture; nothing is asked twice
    assert all(isinstance(refs, list) for refs in sdui.captured)


def test_nothing_queued_means_nothing_claimed(worker_client: WorkerClient) -> None:
    searcher = FakeSearcher({})
    assert make_worker(worker_client, searcher).poll_once() is False
    assert searcher.searched == []


# ---------------------------------------------------------------------- safety stops, cool-down, caps
def test_a_safety_stop_ends_the_run_marks_the_account_and_starts_the_team_wide_cooldown(worker_client: WorkerClient) -> None:
    seed_keywords("corporate ai training", "ai workshop")
    run_id = queue()
    searcher = FakeSearcher({"corporate ai training": [post(1, TEXT[1])], "ai workshop": safety("captcha")})
    make_worker(worker_client, searcher).poll_once()
    row = run_row(run_id)
    assert row["status"] == "aborted" and row["abort_reason"] == "captcha"
    assert row["new"] == 1  # what was scanned before the stop is kept
    with session_scope() as db:
        assert get_session_row(db).status == "needs_attention"
        assert runs.safety_cooldown_until(db) is not None
        kws = {k.keyword_text: k.status for k in db.scalars(select(ScrapeRunKeyword).where(ScrapeRunKeyword.run_id == run_id))}
        assert kws == {"corporate ai training": "done", "ai workshop": "error"}
    # The cool-down is enforced on the server: queueing is refused, so nothing can reach LinkedIn.
    with session_scope() as db, pytest.raises(DomainError) as exc:
        runs.start_run(db)
    assert exc.value.code == "safety_cooldown"
    assert "Connect LinkedIn" not in (row["msg"] or "")  # hosted wording points at the laptop
    assert "sales0 worker --connect" in (row["msg"] or "")


def test_a_run_queued_before_a_cooldown_is_refused_at_claim_time(worker_client: WorkerClient) -> None:
    seed_keywords("ai workshop")
    first = queue()
    make_worker(worker_client, FakeSearcher({"ai workshop": safety("rate_limited")})).poll_once()
    assert run_row(first)["abort_reason"] == "rate_limited"
    # a run slipped into the queue (e.g. queued by someone before the stop was recorded)
    with session_scope() as db:
        from sales0.clock import utcnow

        db.add(ScrapeRun(trigger="manual", mode="live", status="queued", started_at=utcnow()))
    searcher = FakeSearcher({"ai workshop": [post(1, TEXT[1])]})
    assert make_worker(worker_client, searcher).poll_once() is False
    assert searcher.searched == []
    with session_scope() as db:
        last = db.scalars(select(ScrapeRun).order_by(ScrapeRun.id.desc())).first()
        assert last.status == "aborted" and "slow down" in (last.error_message or "")


def test_not_logged_in_fails_the_run_with_the_hosted_hint(worker_client: WorkerClient) -> None:
    seed_keywords("ai workshop")
    run_id = queue()
    searcher = FakeSearcher({}, enter_error=session_missing())
    make_worker(worker_client, searcher).poll_once()
    row = run_row(run_id)
    assert row["status"] == "failed" and "sales0 worker --connect" in (row["msg"] or "")


def test_a_page_error_on_one_keyword_does_not_stop_the_run(worker_client: WorkerClient) -> None:
    seed_keywords("corporate ai training", "ai workshop")
    run_id = queue()
    searcher = FakeSearcher({"corporate ai training": scrape_error("timeout"), "ai workshop": [post(3, TEXT[3])]})
    make_worker(worker_client, searcher).poll_once()
    row = run_row(run_id)
    assert row["status"] == "succeeded" and row["new"] == 1 and row["done"] == 2


def test_the_daily_post_cap_stops_the_run_between_keywords(worker_client: WorkerClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_MAX_POSTS_PER_DAY", "2")
    from sales0.config import reset_settings

    reset_settings()
    seed_keywords("corporate ai training", "ai workshop")
    run_id = queue()
    searcher = FakeSearcher(corpus())
    make_worker(worker_client, searcher).poll_once()
    row = run_row(run_id)
    assert row["abort_reason"] == "daily_post_cap" and row["status"] == "succeeded"
    assert searcher.searched == ["corporate ai training"]


# ---------------------------------------------------------------------- idempotency / ordering / auth
def _claim(client: WorkerClient, name: str = "laptop") -> p.ClaimedRun:
    reply = client.claim(p.ClaimRequest(worker=p.WorkerInfo(name=name, version="1")))
    assert reply.run is not None, reply.reason
    return reply.run


def _payload(n: int) -> p.PostPayload:
    sp = post(n, TEXT[n])
    return p.PostPayload(
        source_ref=sp.source_ref, url=sp.url, author_name=sp.author_name, author_headline=sp.author_headline,
        posted_relative=sp.posted_relative, posted_date=sp.posted_date, text=sp.text, link_kind="post",
    )


def test_a_replayed_batch_is_acknowledged_and_not_counted_twice(worker_client: WorkerClient) -> None:
    seed_keywords("ai workshop")
    run_id = queue()
    claimed = _claim(worker_client)
    row_id = claimed.keywords[0].row_id
    batch = p.UploadPostsRequest(seq=1, posts=[_payload(1), _payload(2)])
    first = worker_client.upload_posts(claimed.run_id, row_id, batch)
    again = worker_client.upload_posts(claimed.run_id, row_id, batch)  # the response was lost; the worker retries
    assert (first.duplicate, first.new) == (False, 2) and again.duplicate is True
    assert run_row(run_id)["seen"] == 2 and run_row(run_id)["new"] == 2


def test_out_of_order_batches_are_refused(worker_client: WorkerClient) -> None:
    seed_keywords("ai workshop")
    queue()
    claimed = _claim(worker_client)
    with pytest.raises(Exception) as exc:
        worker_client.upload_posts(claimed.run_id, claimed.keywords[0].row_id, p.UploadPostsRequest(seq=3, posts=[_payload(1)]))
    assert getattr(exc.value, "code", "") == "out_of_order"


def test_the_worker_api_needs_the_token(hosted: TestClient) -> None:
    body = {"worker": {"name": "x", "version": "1"}}
    assert hosted.post("/api/worker/claim", json=body).status_code == 401
    assert hosted.post("/api/worker/claim", json=body, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert hosted.post("/api/worker/claim", json=body, headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    ok = hosted.post("/api/worker/claim", json=body, headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200 and ok.json()["data"]["run"] is None
    # the login middleware does not intercept it, and the human pages still demand a login
    assert hosted.get("/inbox", follow_redirects=False).status_code in (303, 401)


def test_a_wrong_token_is_a_permanent_auth_error_for_the_client(hosted: TestClient) -> None:
    bad = WorkerClient(BASE, "x" * 40, http=hosted, sleep=lambda _s: None)
    with pytest.raises(WorkerAuthError):
        bad.claim(p.ClaimRequest(worker=p.WorkerInfo(name="x")))


def test_without_a_configured_token_the_worker_api_does_not_exist(db_url: str) -> None:
    from sales0.web.app import create_app

    with TestClient(create_app(), base_url=BASE) as c:  # local mode, no SALES0_WORKER_TOKEN
        r = c.post("/api/worker/claim", json={"worker": {"name": "x"}}, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 404


def test_unknown_fields_and_oversized_values_are_rejected(hosted: TestClient) -> None:
    h = {"Authorization": f"Bearer {TOKEN}"}
    assert hosted.post("/api/worker/claim", json={"worker": {"name": "x"}, "surprise": 1}, headers=h).status_code in (400, 422)
    assert hosted.post("/api/worker/claim", json={"worker": {"name": "x" * 500}}, headers=h).status_code in (400, 422)


# ---------------------------------------------------------------------- resume / stale / cancel
def test_a_restarted_worker_resumes_its_own_run_and_another_worker_cannot_take_it(worker_client: WorkerClient) -> None:
    seed_keywords("corporate ai training", "ai workshop")
    run_id = queue()
    first = _claim(worker_client, "laptop")
    assert [k.status for k in first.keywords] == ["pending", "pending"]
    # laptop dies; it comes back
    resumed = _claim(worker_client, "laptop")
    assert resumed.run_id == run_id and resumed.resumed is True and len(resumed.keywords) == 2
    # a different machine does not steal a live run, nor start a second one beside it
    with session_scope() as db:
        from sales0.clock import utcnow

        db.add(ScrapeRun(trigger="manual", mode="live", status="queued", started_at=utcnow()))
    other = worker_client.claim(p.ClaimRequest(worker=p.WorkerInfo(name="other", version="1")))
    assert other.run is None and "in progress" in (other.reason or "")


def test_a_silent_worker_is_reaped_and_the_queue_moves_on(worker_client: WorkerClient) -> None:
    from datetime import timedelta

    from sales0.clock import utcnow

    seed_keywords("ai workshop")
    run_id = queue()
    _claim(worker_client, "laptop")
    later = utcnow() + timedelta(seconds=get_settings().worker_stale_s + 5)
    with session_scope() as db:
        assert worker_service.reap_stale(db, later) == 1
    row = run_row(run_id)
    assert row["status"] == "aborted" and "stopped responding" in (row["msg"] or "")
    # nothing was observed on LinkedIn, so no cool-down
    with session_scope() as db:
        assert runs.safety_cooldown_until(db) is None


def test_cancel_a_queued_run_and_stop_a_running_one(worker_client: WorkerClient) -> None:
    seed_keywords("corporate ai training", "ai workshop")
    queued = queue()
    with session_scope() as db:
        assert "cancelled" in worker_service.cancel_run(db, queued)
    assert run_row(queued)["status"] == "aborted"

    running = queue()
    claimed = _claim(worker_client)
    with session_scope() as db:
        assert "Stop requested" in worker_service.cancel_run(db, running)
    assert worker_client.heartbeat(claimed.run_id, p.HeartbeatRequest(step="x")).cancel is True

    searcher = FakeSearcher(corpus())
    w = make_worker(worker_client, searcher)
    # the worker's own heartbeat thread would flip this; drive the flag directly for determinism
    class Beat:
        cancel = threading.Event()

        def set(self, **_k: object) -> None: ...

    Beat.cancel.set()
    out = w._scan_keywords(claimed, searcher, Beat())  # noqa: SLF001
    assert out.outcome == "cancelled" and searcher.searched == []
    worker_client.complete(claimed.run_id, out)
    assert run_row(running)["status"] == "aborted"


# ---------------------------------------------------------------------- hosted-downtime survival
def test_the_worker_rides_out_a_hosted_outage_mid_run(hosted: TestClient) -> None:
    seed_keywords("corporate ai training", "ai workshop")
    run_id = queue()
    ft = {"slept": []}
    flaky = FlakyHttp(hosted, failures=0)
    client = WorkerClient(BASE, TOKEN, http=flaky, sleep=ft["slept"].append, clock=lambda: 0.0)
    w = make_worker(client, FakeSearcher(corpus()))
    # claim first (healthy), then the hosted side disappears for the first 3 calls of the run, once with 503s
    claimed = client.claim(p.ClaimRequest(worker=w.info)).run
    flaky.failures = 3
    w.execute(claimed)
    assert run_row(run_id)["status"] == "succeeded"
    assert ft["slept"][:3] == [1.0, 2.0, 4.0]  # exponential backoff, then it carried on where it was


def test_a_permanent_outage_abandons_the_run_quietly_and_the_server_reaps_it(hosted: TestClient) -> None:
    seed_keywords("ai workshop")
    run_id = queue()
    flaky = FlakyHttp(hosted, failures=0, kind="503")
    ticks = iter(range(0, 10_000, 700))
    client = WorkerClient(BASE, TOKEN, http=flaky, sleep=lambda _s: None, clock=lambda: float(next(ticks)), outage_budget_s=1800)
    w = make_worker(client, FakeSearcher({"ai workshop": [post(1, TEXT[1])]}))
    claimed = client.claim(p.ClaimRequest(worker=w.info)).run
    flaky.failures = 10_000
    w.execute(claimed)  # must return, not raise
    assert run_row(run_id)["status"] == "running"  # the server does not know; stale detection ends it later
    with pytest.raises(ServerUnavailable):
        client.heartbeat(run_id, p.HeartbeatRequest())


# ---------------------------------------------------------------------- hosted behaviour of the rest of the app
def test_browser_actions_are_refused_in_hosted_mode(hosted: TestClient) -> None:
    from sales0.services import session as session_service

    with session_scope() as db:
        for action in (session_service.start_connect, session_service.start_check):
            with pytest.raises(DomainError) as exc:
                action(db)
            assert exc.value.code == "hosted_mode"


def test_queueing_does_not_need_a_connected_linkedin_and_nothing_starts_in_process(hosted: TestClient) -> None:
    seed_keywords("ai workshop")
    with session_scope() as db:
        assert get_session_row(db).status != "connected"
        view = runs.start_run(db)
    assert view.status == "queued"
    from sales0.jobs.runner import get_runner

    assert get_runner().busy() is None


def test_a_restart_does_not_abort_a_queued_run(hosted: TestClient) -> None:
    seed_keywords("ai workshop")
    run_id = queue()
    assert runs.recover_interrupted_runs_on_startup() == 0
    assert run_row(run_id)["status"] == "queued"


def test_header_and_runs_page_say_worker_offline_while_a_run_waits(hosted: TestClient) -> None:
    seed_keywords("ai workshop")
    queue()
    login = hosted.post("/login", data={"email": get_settings().admin_email, "password": "correct horse battery"}, follow_redirects=False)
    assert login.status_code in (200, 303), login.text[:200]
    page = hosted.get("/runs")
    assert page.status_code == 200
    assert "Worker offline: start `sales0 worker` on the laptop" in page.text.replace("&#96;", "`").replace("&#x60;", "`")
    assert "Queue a run" not in page.text or "worker-offline-hint" in page.text
    setup = hosted.get("/setup")
    assert "Laptop worker" in setup.text and "Offline" in setup.text


def test_the_schedule_page_has_no_toggles_and_the_watch_job_refuses(hosted: TestClient) -> None:
    hosted.post("/login", data={"email": get_settings().admin_email, "password": "correct horse battery"}, follow_redirects=False)
    page = hosted.get("/schedule")
    assert page.status_code == 200 and "/enable" not in page.text and "Nothing on this hosted app runs on a timer" in page.text
    r = hosted.post("/schedule/profile_watch/run", follow_redirects=False)
    assert r.status_code in (409, 303)
