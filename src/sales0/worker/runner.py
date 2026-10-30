"""The laptop worker loop: claim a queued run, scrape it with the local logged-in Chrome, upload raw posts.

The hosted app owns everything that is data (dedupe, Haiku triage, counters, caps, cool-down). The worker owns
only what needs the user's browser: ``LinkedInSearcher.scan`` and ``capture_links``. LinkedIn cookies and the
Chrome profile never leave this machine; only the posts a scan returned are uploaded.

Per keyword:  scan -> upload posts in batches (non-final) -> capture permalinks the server asked for ->
upload them -> a final empty batch carrying the scan facts (the server finishes the keyword and answers whether
to continue). Every upload carries a per-keyword ``seq`` so a retry after a lost response is a no-op.
"""

from __future__ import annotations

import contextlib
import random
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from sales0 import __version__
from sales0.config import Settings
from sales0.logging import get_logger
from sales0.scraper.errors import AccountSafetyStop, BrowserUnavailable, ScrapeError, SessionMissing
from sales0.worker import protocol as p
from sales0.worker.client import ServerUnavailable, WorkerApiError, WorkerAuthError, WorkerClient

log = get_logger("sales0.worker")

SearcherFactory = Callable[[Settings, int], Any]  # (settings, page_load_budget) -> context manager yielding a searcher
PageReader = Callable[[Any, str], Any]  # (open searcher, "company/<x>") -> object with title / url / text
CAPABILITIES: list[Any] = ["company_page"]  # q15: one-page LinkedIn company tasks (prospect research)
MAX_BATCH_BYTES = 700_000  # the hosted body cap is 1 MB; stay well under it


def default_searcher_factory(settings: Settings, page_load_budget: int) -> Any:
    from sales0.scraper.search import LinkedInSearcher

    return LinkedInSearcher(settings, account=None, page_load_budget=page_load_budget)


def default_page_reader(searcher: Any, slug: str) -> Any:
    from sales0.scraper.company_page import read_company_page

    return read_company_page(searcher, slug)


def _cap(value: str | None, limit: int) -> str | None:
    return value[:limit] if value else value


def post_payload(post: Any) -> p.PostPayload:
    """A ``ScrapedPost`` (or any object with the same attributes) as its wire form. Over-long values are clipped,
    never rejected: one odd post must not fail the whole upload (triage reads only the first few thousand chars)."""
    return p.PostPayload(
        source_ref=post.source_ref[:300],
        url=_cap(post.url, 2000) or "",
        author_name=_cap(post.author_name, 300),
        author_profile_url=_cap(post.author_profile_url, 2000),
        author_headline=_cap(post.author_headline, 600),
        posted_relative=_cap(post.posted_relative, 100),
        posted_date=post.posted_date,
        text=_cap(post.text, 60_000) or "",
        link_kind=getattr(post, "link_kind", "post"),
        search_url=_cap(getattr(post, "search_url", None), 2000),
        truncated=bool(getattr(post, "truncated", False)),
        raw_card=getattr(post, "raw_card", None),
    )


def batches(posts: list[p.PostPayload], max_posts: int) -> list[list[p.PostPayload]]:
    """Split into upload batches of at most ``max_posts`` posts and ~``MAX_BATCH_BYTES`` of JSON."""
    out: list[list[p.PostPayload]] = []
    current: list[p.PostPayload] = []
    size = 0
    for post in posts:
        approx = len(post.model_dump_json())
        if current and (len(current) >= max_posts or size + approx > MAX_BATCH_BYTES):
            out.append(current)
            current, size = [], 0
        current.append(post)
        size += approx
    if current:
        out.append(current)
    return out


class _Heartbeat:
    """Sends a heartbeat every ``interval_s`` while the main thread is busy inside a (minutes-long) scan."""

    def __init__(self, client: WorkerClient, run_id: int, interval_s: float) -> None:
        self._client, self._run_id, self._interval = client, run_id, interval_s
        self._lock = threading.Lock()
        self._state = p.HeartbeatRequest()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="sales0-worker-heartbeat", daemon=True)
        self.cancel = threading.Event()

    def set(self, *, step: str | None = None, keyword: str | None = None, page_loads: int | None = None) -> None:
        with self._lock:
            self._state = p.HeartbeatRequest(
                step=step if step is not None else self._state.step,
                keyword=keyword if keyword is not None else self._state.keyword,
                page_loads=page_loads if page_loads is not None else self._state.page_loads,
            )

    def beat(self) -> None:
        with self._lock:
            state = self._state
        try:
            if self._client.heartbeat(self._run_id, state).cancel:
                self.cancel.set()
        except (ServerUnavailable, WorkerApiError) as exc:
            log.warning("worker_heartbeat_failed", error=type(exc).__name__)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.beat()

    def __enter__(self) -> _Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(2.0)


class Worker:
    def __init__(
        self,
        settings: Settings,
        client: WorkerClient,
        *,
        searcher_factory: SearcherFactory = default_searcher_factory,
        page_reader: PageReader = default_page_reader,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.factory = searcher_factory
        self.page_reader = page_reader
        self._send_capabilities = True  # dropped (once) when an older hosted app rejects the field
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.stop = stop or threading.Event()
        self._base_loads = 0
        self.info = p.WorkerInfo(
            name=(settings.worker_name or socket.gethostname() or "worker")[:80],
            version=__version__,
            host=socket.gethostname()[:120],
        )

    # ------------------------------------------------------------------ loop
    def run_forever(self) -> None:
        idle_failures = 0
        log.info("worker_started", server=self.settings.worker_server_url, name=self.info.name)
        while not self.stop.is_set():
            try:
                worked = self.poll_once()
                idle_failures = 0
            except WorkerAuthError as exc:
                log.error("worker_auth_failed", message=exc.message)
                print(f"sales0 worker: the hosted app refused the token ({exc.message}). Check SALES0_WORKER_TOKEN.")
                return
            except (ServerUnavailable, WorkerApiError) as exc:
                idle_failures += 1
                wait = min(self.settings.worker_poll_s * (2 ** min(idle_failures, 5)), 300.0)
                log.warning("worker_poll_failed", error=type(exc).__name__, wait_s=wait)
                self._wait(wait)
                continue
            if not worked:
                self._wait(self.settings.worker_poll_s)

    def _wait(self, seconds: float) -> None:
        self.stop.wait(seconds)

    def poll_once(self) -> bool:
        """Ask for work; run it when there is some. True when a run was executed."""
        reply = self._claim()
        if reply.task is not None:
            self.execute_task(reply.task)
            return True
        if reply.run is None:
            if reply.reason:
                log.info("worker_idle", reason=reply.reason)
            return False
        self.execute(reply.run)
        return True

    def _claim(self) -> p.ClaimResponse:
        if self._send_capabilities:
            try:
                return self.client.claim(p.ClaimRequest(worker=self.info, capabilities=list(CAPABILITIES)))
            except WorkerApiError as exc:
                if exc.status != 422 or isinstance(exc, WorkerAuthError):
                    raise
                self._send_capabilities = False  # an older hosted app: keyword runs only
                log.warning("worker_capabilities_rejected", message=exc.message[:120])
        return self.client.claim(p.ClaimRequest(worker=self.info))

    # ------------------------------------------------------------------ one task (q15)
    def execute_task(self, task: p.ClaimedTask) -> None:
        """Visit ONE LinkedIn page for the hosted app and upload its text. The browser is opened with a budget of
        one page load; every safety stop is reported so the hosted app pauses the account."""
        log.info("worker_task_claimed", task_id=task.task_id, kind=task.kind)
        searcher: Any = None
        try:
            with self.factory(self.settings, 1) as searcher:
                page = self.page_reader(searcher, task.slug)
            result = p.TaskResultRequest(
                outcome="done", title=_cap(getattr(page, "title", None), 300), url=_cap(getattr(page, "url", None), 1000),
                text=_cap(getattr(page, "text", None), 30_000), page_loads=_loads(searcher),
            )  # fmt: skip
        except SessionMissing:
            result = p.TaskResultRequest(outcome="session_missing")
        except AccountSafetyStop as stop:
            result = p.TaskResultRequest(outcome="safety_stop", kind=stop.kind, page_loads=_loads(searcher))
        except BrowserUnavailable as exc:
            result = p.TaskResultRequest(outcome="open_failed", error=str(exc)[:300])
        except ScrapeError as exc:
            result = p.TaskResultRequest(outcome="page_error", error=str(exc)[:300], page_loads=_loads(searcher))
        except Exception as exc:  # noqa: BLE001 - never leave the task hanging
            log.exception("worker_task_crashed", task_id=task.task_id)
            result = p.TaskResultRequest(outcome="crashed", error=type(exc).__name__)
        try:
            done = self.client.task_result(task.task_id, result)
            log.info("worker_task_completed", task_id=task.task_id, status=done.status)
        except (ServerUnavailable, WorkerApiError) as exc:
            log.error("worker_task_result_failed", task_id=task.task_id, error=type(exc).__name__)

    # ------------------------------------------------------------------ one run
    def execute(self, run: p.ClaimedRun) -> None:
        settings = self.settings
        log.info("worker_run_claimed", run_id=run.run_id, keywords=len(run.keywords), resumed=run.resumed)
        outcome: p.CompleteRequest | None = None
        searcher: Any = None
        self._base_loads = run.page_loads_so_far  # a resumed run: the earlier browser's page loads still count
        with _Heartbeat(self.client, run.run_id, settings.worker_heartbeat_s) as beat:
            beat.set(step="Opening LinkedIn and checking the session")
            try:
                with self.factory(settings, run.caps.page_loads_left_today) as searcher:
                    outcome = self._scan_keywords(run, searcher, beat)
            except SessionMissing:
                outcome = p.CompleteRequest(outcome="session_missing")
            except AccountSafetyStop as stop:
                outcome = p.CompleteRequest(outcome="safety_stop", kind=stop.kind, url=stop.url)
            except BrowserUnavailable as exc:
                outcome = p.CompleteRequest(outcome="open_failed", error=f"Could not open the browser: {exc}"[:500])
            except ScrapeError as exc:
                outcome = p.CompleteRequest(outcome="open_failed", error=f"Could not open LinkedIn: {exc}"[:500])
            except ServerUnavailable:
                log.error("worker_run_abandoned", run_id=run.run_id, reason="hosted app unreachable")
                return  # the hosted side marks the run lost after SALES0_WORKER_STALE_S; the next claim resumes it
            except WorkerApiError as exc:
                if exc.code in ("run_not_running", "not_found"):
                    log.warning("worker_run_ended_by_server", run_id=run.run_id, code=exc.code)
                    return  # an admin cancelled it / the server reaped it: nothing left to report
                log.exception("worker_run_crashed", run_id=run.run_id)
                outcome = p.CompleteRequest(outcome="crashed", error=f"Worker error ({exc.code})")
            except Exception as exc:  # noqa: BLE001 - never leave the run hanging
                log.exception("worker_run_crashed", run_id=run.run_id)
                outcome = p.CompleteRequest(outcome="crashed", error=f"Worker error ({type(exc).__name__})")
        loads = _loads(searcher, self._base_loads)
        if loads is not None and outcome.page_loads is None:
            outcome = outcome.model_copy(update={"page_loads": loads})
        try:
            done = self.client.complete(run.run_id, outcome)
            log.info("worker_run_completed", run_id=run.run_id, status=done.status)
        except ServerUnavailable:
            log.error("worker_complete_failed", run_id=run.run_id)

    def _scan_keywords(self, run: p.ClaimedRun, searcher: Any, beat: _Heartbeat) -> p.CompleteRequest:
        caps = run.caps
        todo = [k for k in run.keywords if k.status in ("pending", "running")]
        current: p.PlannedKeyword | None = None
        try:
            for index, kw in enumerate(todo, start=1):
                current = kw
                if beat.cancel.is_set() or self.stop.is_set():
                    return p.CompleteRequest(outcome="cancelled")
                if index > 1:
                    seconds = self.rng.uniform(caps.keyword_pause_min_s, caps.keyword_pause_max_s)
                    beat.set(step=f"Pausing {round(seconds)}s before next keyword")
                    self.sleep(seconds)
                beat.set(step=f"Searching '{kw.text}' ({index}/{len(todo)})", keyword=kw.text,
                         page_loads=_loads(searcher, self._base_loads))
                ack = self._one_keyword(run.run_id, kw, searcher, caps, beat)
                beat.set(page_loads=_loads(searcher, self._base_loads))
                if ack.caps is not None:
                    caps = ack.caps
                if not ack.continue_run:
                    if ack.stop_kind in ("post", "page"):
                        return p.CompleteRequest(outcome="cap_hit", cap_kind=ack.stop_kind)
                    return p.CompleteRequest(outcome="finished")  # llm: the server fails the run with its own message
            return p.CompleteRequest(outcome="finished")
        except AccountSafetyStop as stop:
            return p.CompleteRequest(
                outcome="safety_stop", kind=stop.kind, url=stop.url, row_id=current.row_id if current else None
            )

    def _one_keyword(self, run_id: int, kw: p.PlannedKeyword, searcher: Any, caps: p.RunCaps, beat: _Heartbeat) -> p.KeywordAck:
        max_posts = max(1, min(caps.max_posts_per_keyword, caps.posts_left_today))
        seq = 0
        started = time.monotonic()
        try:
            result = searcher.scan(kw.text, max_posts)
        except AccountSafetyStop:
            raise
        except ScrapeError as exc:
            meta = p.KeywordResultMeta(scan_error=f"Page error: {exc}"[:300], page_loads_total=_loads(searcher, self._base_loads))
            return self.client.upload_posts(run_id, kw.row_id, p.UploadPostsRequest(seq=1, final=True, result=meta))
        payloads = [post_payload(post) for post in result.posts]
        beat.set(step=f"Uploading {len(payloads)} posts for '{kw.text}'")
        link_refs: list[str] = []
        ack = p.KeywordAck()
        for chunk in batches(payloads, self.settings.worker_upload_batch):
            seq += 1
            ack = self.client.upload_posts(run_id, kw.row_id, p.UploadPostsRequest(seq=seq, posts=chunk))
            link_refs.extend(r for r in ack.link_refs if r not in link_refs)
            if not ack.continue_run:
                break
        stopped = not ack.continue_run
        if link_refs and not stopped and hasattr(searcher, "capture_links"):
            beat.set(step=f"Copying post links for '{kw.text}'")
            safety: AccountSafetyStop | None = None
            captured: Any = None
            try:
                captured = searcher.capture_links(link_refs)
            except AccountSafetyStop as stop:
                safety = stop
            except ScrapeError as exc:
                log.warning("capture_links_failed", error=str(exc)[:200])
            if captured is not None and getattr(captured, "links", None):
                self.client.upload_links(
                    run_id, kw.row_id,
                    p.UploadLinksRequest(links=dict(captured.links), attempts=int(getattr(captured, "attempts", 0) or 0)),
                )
            if safety is not None:
                self._final(run_id, kw, result, searcher, seq + 1, started)  # everything scanned so far is kept
                raise safety
        return self._final(run_id, kw, result, searcher, seq + 1, started)

    def _final(
        self, run_id: int, kw: p.PlannedKeyword, result: Any, searcher: Any, seq: int, started: float
    ) -> p.KeywordAck:
        meta = p.KeywordResultMeta(
            scrolls=int(getattr(result, "scrolls", 0) or 0),
            parse_errors=int(getattr(result, "parse_errors", 0) or 0),
            drift_warning=bool(getattr(result, "drift_warning", False)),
            cards_found=int(getattr(result, "cards_found", 0) or 0),
            no_results=bool(getattr(result, "no_results", False)),
            field_misses={str(k): int(v) for k, v in (getattr(result, "field_misses", None) or {}).items()},
            strategy=str(getattr(result, "strategy", None) or "none"),
            passes=int(getattr(result, "passes", 1) or 1),
            expanded=int(getattr(result, "expanded", 0) or 0),
            stop_reason=str(getattr(result, "stop_reason", None) or "cap"),
            page_loads=int(getattr(result, "page_loads", 0) or 0),
            page_loads_total=_loads(searcher, self._base_loads),
            duration_ms=int((time.monotonic() - started) * 1000),
            layout_sample=_sample(result),
        )
        return self.client.upload_posts(run_id, kw.row_id, p.UploadPostsRequest(seq=seq, final=True, result=meta))


def _sample(result: Any) -> p.LayoutSamplePayload | None:
    raw = getattr(result, "layout_sample", None)
    if not isinstance(raw, dict):
        return None
    try:
        return p.LayoutSamplePayload.model_validate({**raw, "html": str(raw.get("html") or "")[:130_000]})
    except Exception:  # noqa: BLE001 - a malformed sample must never cost the keyword's upload
        log.warning("layout_sample_dropped")
        return None


def _loads(searcher: Any, base: int = 0) -> int | None:
    value = getattr(searcher, "page_loads", None)
    return base + value if isinstance(value, int) and not isinstance(value, bool) else None


def run_worker(settings: Settings) -> int:
    """Entry point of ``sales0 worker``: validate configuration, then loop until Ctrl-C."""
    if not settings.worker_server_url:
        print("sales0 worker: set SALES0_WORKER_SERVER_URL to the hosted app's URL.")
        return 2
    if settings.worker_token is None:
        print("sales0 worker: set SALES0_WORKER_TOKEN (the same token the hosted app was deployed with).")
        return 2
    from urllib.parse import urlsplit

    parts = urlsplit(settings.worker_server_url)
    if parts.scheme != "https" and (parts.hostname or "") not in ("127.0.0.1", "localhost", "::1"):
        print("sales0 worker: SALES0_WORKER_SERVER_URL must be https:// (the bearer token would travel in clear text).")
        return 2
    client = WorkerClient(settings.worker_server_url, settings.worker_token_value or "")
    worker = Worker(settings, client)
    print(f"sales0 worker '{worker.info.name}' polling {settings.worker_server_url} every {settings.worker_poll_s:g}s. Ctrl-C to stop.")
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop.set()
        print("sales0 worker: stopped.")
    finally:
        with contextlib.suppress(Exception):
            client.close()
    return 0


__all__ = ["Worker", "batches", "post_payload", "run_worker"]
