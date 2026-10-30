"""Hosted side of the laptop worker (``/api/worker/*``).

Cloud Run gives CPU only during a request and scales to zero, so no discovery run can live in the hosted process.
Instead the run is a *row* that the worker drives through short requests:

* ``claim``      - hand the oldest queued run to the worker (plans its keywords like the local job does);
* ``heartbeat``  - progress text, page-load count, the Stop flag;
* ``upload_posts`` - raw posts of one keyword in batches. Each batch is registered (dedupe on fingerprint),
                  counted and triaged by Haiku INSIDE the request (the same ``discovery_job._Run`` the local job
                  uses), so what the team sees is identical in both modes. The last batch (``final``) closes the
                  keyword and answers whether to continue;
* ``upload_links`` - permalinks the worker captured for the posts the previous answer asked about;
* ``complete``   - the worker's verdict (finished / safety stop / not logged in / ...), turned into the same run
                  status, cool-down and account state as the local job.

Idempotency: every post batch carries a per-keyword ``seq`` (stored in ``scrape_run_keywords.upload_seq``); a retry
of an already-applied batch is acknowledged without being re-applied.
Crash safety: a claimed run whose worker stops heartbeating for ``SALES0_WORKER_STALE_S`` is marked lost
(``reap_stale``); the same worker claiming again RESUMES its own running run.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc, utcnow
from sales0.config import Settings, get_settings
from sales0.db.models import ScrapeRun, ScrapeRunKeyword, SeenPost, Worker
from sales0.errors import DomainError
from sales0.jobs import discovery_job
from sales0.llm import LLMClient
from sales0.logging import get_logger
from sales0.scraper.types import ScrapedPost
from sales0.services import linkedin_accounts, seen_posts
from sales0.services import runs as runs_service
from sales0.services._common import clip, not_found, now_or, record_history
from sales0.worker import protocol as p

log = get_logger("sales0.worker_service")

LOST_MESSAGE = "The laptop worker stopped responding - the run was ended. The next run continues with the keywords it had not reached."
ONLINE_WINDOW_S = 90  # a worker polls every ~15 s; silent for longer than this = offline


# ---------------------------------------------------------------------- worker registry / status
def upsert_worker(db: Session, info: p.WorkerInfo, linkedin: p.LinkedInReport | None, now: datetime) -> Worker:
    row = db.scalar(select(Worker).where(Worker.name == info.name))
    if row is None:
        row = Worker(name=info.name, first_seen_at=now, last_seen_at=now)
        db.add(row)
    row.version, row.host, row.protocol, row.last_seen_at = info.version, info.host, info.protocol, now
    if linkedin is not None and linkedin.status != "unknown":
        row.linkedin_status, row.linkedin_detail = linkedin.status, clip(linkedin.detail, 300)
    db.flush()
    return row


@dataclass(frozen=True)
class WorkerStatusView:
    name: str | None
    version: str | None
    host: str | None
    last_seen_at: datetime | None
    online: bool
    queued_runs: int
    waiting_since: datetime | None  # the oldest queued run, when no worker is online
    offline_hint: str | None


OFFLINE_HINT = "Worker offline: start `sales0 worker` on the laptop (it needs SALES0_WORKER_SERVER_URL and SALES0_WORKER_TOKEN)."


def worker_status(db: Session, now: datetime | None = None) -> WorkerStatusView:
    now = now_or(now)
    row = db.scalar(select(Worker).order_by(Worker.last_seen_at.desc()).limit(1))
    online = row is not None and (now - ensure_utc(row.last_seen_at)).total_seconds() <= ONLINE_WINDOW_S
    oldest = db.scalar(select(func.min(ScrapeRun.started_at)).where(ScrapeRun.status == "queued"))
    queued = int(db.scalar(select(func.count(ScrapeRun.id)).where(ScrapeRun.status == "queued")) or 0)
    return WorkerStatusView(
        name=row.name if row else None,
        version=row.version if row else None,
        host=row.host if row else None,
        last_seen_at=row.last_seen_at if row else None,
        online=online,
        queued_runs=queued,
        waiting_since=oldest if queued and not online else None,
        offline_hint=OFFLINE_HINT if not online else None,
    )


# ---------------------------------------------------------------------- stale runs / cancel
def reap_stale(db: Session, now: datetime | None = None, *, keep_worker: str | None = None) -> int:
    """End runs whose worker went silent. ``keep_worker``: that worker's own run is resumed, not reaped."""
    now = now_or(now)
    cutoff = now - timedelta(seconds=get_settings().worker_stale_s)
    stale = db.scalars(
        select(ScrapeRun).where(
            ScrapeRun.status == "running",
            ScrapeRun.worker_name.is_not(None),
            ScrapeRun.heartbeat_at < cutoff,
        )
    ).all()
    count = 0
    for run in stale:
        if keep_worker is not None and run.worker_name == keep_worker:
            continue
        _end_lost(db, run, now)
        count += 1
    if count:
        db.commit()
        log.warning("worker_runs_reaped", count=count)
    return count


def _end_lost(db: Session, run: ScrapeRun, now: datetime) -> None:
    record_history(db, "scrape_run", run.id, "status", run.status, "aborted", note=LOST_MESSAGE, now=now)
    run.status = "aborted"
    run.abort_reason = None  # no cool-down: nothing LinkedIn-side was observed
    run.error_message = LOST_MESSAGE[:500]
    run.finished_at = now
    run.current_step = "Worker lost - run ended"
    for row in db.scalars(select(ScrapeRunKeyword).where(ScrapeRunKeyword.run_id == run.id)).all():
        if row.status in ("pending", "running"):
            row.status = "skipped"
            row.error_message = "Not finished - worker lost"[:300]


def cancel_run(db: Session, run_id: int, *, actor_user_id: int | None = None) -> str:
    """Admin Stop. A queued run is dropped at once; a claimed one is asked to stop at its next heartbeat."""
    run = db.get(ScrapeRun, run_id)
    if run is None:
        raise not_found()
    now = utcnow()
    if run.status == "queued":
        record_history(db, "scrape_run", run.id, "status", "queued", "aborted", note="cancelled", actor_user_id=actor_user_id, now=now)
        run.status, run.finished_at, run.error_message = "aborted", now, "Cancelled before a worker picked it up."
        run.current_step = "Cancelled"
        db.commit()
        return "The queued run was cancelled."
    if run.status == "running":
        run.cancel_requested = True
        db.commit()
        return "Stop requested - the worker finishes its current page and ends the run."
    raise DomainError("run_not_active", "That run has already finished.", 409)


# ---------------------------------------------------------------------- caps
def caps_for(db: Session, run: ScrapeRun, settings: Settings) -> p.RunCaps:
    account_id = run.linkedin_account_id
    return p.RunCaps(
        max_posts_per_keyword=settings.scrape_max_posts_per_keyword,
        posts_left_today=max(0, settings.scrape_max_posts_per_day - runs_service.posts_seen_today(db, account_id=account_id)),
        page_loads_left_today=max(
            0, settings.scrape_max_pageloads_per_day - runs_service.page_loads_today(db, account_id=account_id)
        ),
        keyword_pause_min_s=settings.scrape_keyword_pause_min_s,
        keyword_pause_max_s=settings.scrape_keyword_pause_max_s,
        max_scrolls=settings.scrape_max_scrolls,
    )


def _planned(db: Session, run_id: int) -> list[p.PlannedKeyword]:
    rows = db.scalars(select(ScrapeRunKeyword).where(ScrapeRunKeyword.run_id == run_id).order_by(ScrapeRunKeyword.id)).all()
    return [p.PlannedKeyword(row_id=r.id, text=r.keyword_text, status=r.status) for r in rows]


# ---------------------------------------------------------------------- claim
def claim(db: Session, req: p.ClaimRequest, *, now: datetime | None = None) -> p.ClaimResponse:
    now = now_or(now)
    settings = get_settings()
    worker = upsert_worker(db, req.worker, req.linkedin, now)
    reap_stale(db, now, keep_worker=worker.name)
    poll = settings.worker_poll_s

    mine = db.scalar(
        select(ScrapeRun)
        .where(ScrapeRun.status == "running", ScrapeRun.worker_name == worker.name)
        .order_by(ScrapeRun.id)
        .limit(1)
    )
    if mine is not None:  # the worker restarted mid-run: hand the same run back
        for row in db.scalars(select(ScrapeRunKeyword).where(ScrapeRunKeyword.run_id == mine.id)).all():
            if row.status == "running":
                row.upload_seq = 0  # it is searched again from scratch; its re-sent posts count as already-seen
        mine.heartbeat_at = now
        mine.current_step = "Worker resumed the run"
        worker.last_run_id = mine.id
        db.commit()
        return p.ClaimResponse(
            run=p.ClaimedRun(
                run_id=mine.id, resumed=True, keywords=_planned(db, mine.id), caps=caps_for(db, mine, settings),
                page_loads_so_far=mine.page_loads,
            ),
            poll_s=poll,
        )

    if "company_page" in req.capabilities:  # q15: a one-page task first (a user is waiting on the prospect page)
        from sales0.services.research import linkedin_page

        task = linkedin_page.claim_next(db, worker.name, now)
        if task is not None:
            worker.last_seen_at = now
            db.commit()
            return p.ClaimResponse(task=task, poll_s=poll)

    run = db.scalar(select(ScrapeRun).where(ScrapeRun.status == "queued").order_by(ScrapeRun.id).limit(1))
    if run is None:
        db.commit()
        return p.ClaimResponse(reason="nothing queued", poll_s=poll)
    busy = db.scalar(select(ScrapeRun.id).where(ScrapeRun.status == "running", ScrapeRun.id != run.id).limit(1))
    if busy is not None:
        db.commit()
        return p.ClaimResponse(reason="another run is in progress", poll_s=poll)
    if run.linkedin_account_id is None:
        run.linkedin_account_id = linkedin_accounts.default_account_id(db)
    # The queue may be hours old: caps and the cool-down are checked again NOW, before LinkedIn is touched.
    error, _, _ = runs_service._check(db, now, run.linkedin_account_id, ignore_run_id=run.id)  # noqa: SLF001
    if error is not None and error.code != "run_in_progress":
        record_history(db, "scrape_run", run.id, "status", "queued", "aborted", note=error.code, now=now)
        run.status, run.finished_at, run.error_message = "aborted", now, error.message[:500]
        run.current_step = "Not started - " + error.message[:150]
        db.commit()
        log.info("worker_claim_refused", run_id=run.id, code=error.code)
        return p.ClaimResponse(reason=error.message, poll_s=poll)

    purged = seen_posts.purge_expired(db)
    keywords = discovery_job.select_keywords(db, settings.scrape_max_keywords_per_run)
    if not keywords:
        record_history(db, "scrape_run", run.id, "status", "queued", "failed", note="no_keywords", now=now)
        run.status, run.finished_at, run.error_message = "failed", now, "Add at least one active keyword in Setup."
        db.commit()
        return p.ClaimResponse(reason="no active keywords", poll_s=poll)
    rows = [ScrapeRunKeyword(run_id=run.id, keyword_id=kid, keyword_text=text, status="pending") for kid, text in keywords]
    db.add_all(rows)
    run.keywords_planned = len(rows)
    record_history(db, "scrape_run", run.id, "status", "queued", "running", now=now)
    run.status = "running"
    run.worker_name, run.claimed_at, run.heartbeat_at = worker.name, now, now
    run.current_step = "Worker claimed the run - opening LinkedIn and checking the session"
    worker.last_run_id = run.id
    db.commit()
    log.info("worker_run_claimed", run_id=run.id, worker=worker.name, keywords=len(rows), purged=purged)
    return p.ClaimResponse(
        run=p.ClaimedRun(run_id=run.id, keywords=_planned(db, run.id), caps=caps_for(db, run, settings), page_loads_so_far=run.page_loads),
        poll_s=poll,
    )


# ---------------------------------------------------------------------- heartbeat
def _running(db: Session, run_id: int) -> ScrapeRun:
    run = db.get(ScrapeRun, run_id)
    if run is None:
        raise not_found()
    if run.status != "running":
        raise DomainError("run_not_running", f"Run {run_id} is {run.status}, not running.", 409)
    return run


def heartbeat(db: Session, run_id: int, req: p.HeartbeatRequest, *, now: datetime | None = None) -> p.HeartbeatResponse:
    now = now_or(now)
    run = _running(db, run_id)
    run.heartbeat_at = now
    if req.step:
        run.current_step = clip(req.step, 200)
    if req.keyword:
        run.current_keyword = clip(req.keyword, 100)
    if req.page_loads is not None:
        run.page_loads = max(run.page_loads, req.page_loads)
    if run.worker_name:
        db.execute(
            Worker.__table__.update().where(Worker.name == run.worker_name).values(last_seen_at=now)  # type: ignore[attr-defined]
        )
    cancel = bool(run.cancel_requested)
    db.commit()
    return p.HeartbeatResponse(cancel=cancel)


# ---------------------------------------------------------------------- uploads
def _state(db: Session, run: ScrapeRun, settings: Settings, llm: LLMClient | None) -> discovery_job._Run:  # noqa: SLF001
    state = discovery_job._Run(  # noqa: SLF001
        db, run.id, settings, llm, lambda _s: None, random.Random(), run.linkedin_account_id
    )
    if run.error_message:  # a fatal AI error ended triage earlier in this run (stored as the run's message)
        state.fatal_llm = run.error_message
    return state


def _scraped(post: p.PostPayload) -> ScrapedPost:
    return ScrapedPost(
        source_ref=post.source_ref,
        url=post.url,
        author_name=post.author_name,
        author_profile_url=post.author_profile_url,
        author_headline=post.author_headline,
        posted_relative=post.posted_relative,
        posted_date=post.posted_date,
        text=post.text,
        link_kind=post.link_kind,
        search_url=post.search_url,
        truncated=post.truncated,
        raw_card=post.raw_card,
    )


def _result_of(row: ScrapeRunKeyword, meta: p.KeywordResultMeta) -> SimpleNamespace:
    """The facts ``keyword_outcome`` / the logger read from a ``KeywordScrapeResult``. ``posts`` only needs a length."""
    return SimpleNamespace(
        keyword=row.keyword_text,
        posts=range(row.posts_seen),
        scrolls=meta.scrolls,
        parse_errors=meta.parse_errors,
        drift_warning=meta.drift_warning,
        cards_found=meta.cards_found,
        no_results=meta.no_results,
        field_misses=dict(meta.field_misses),
        strategy=meta.strategy,
        passes=meta.passes,
        expanded=meta.expanded,
        stop_reason=meta.stop_reason,
        page_loads=meta.page_loads,
        layout_sample=meta.layout_sample.model_dump() if meta.layout_sample is not None else None,
    )


def upload_posts(
    db: Session, run_id: int, row_id: int, req: p.UploadPostsRequest, *, llm: LLMClient | None = None, now: datetime | None = None
) -> p.KeywordAck:
    now = now_or(now)
    settings = get_settings()
    run = _running(db, run_id)
    row = db.get(ScrapeRunKeyword, row_id)
    if row is None or row.run_id != run_id:
        raise not_found()
    if req.seq <= row.upload_seq:  # a retry of a batch that was already applied
        return p.KeywordAck(duplicate=True, continue_run=run.error_message is None, caps=caps_for(db, run, settings))
    if req.seq != row.upload_seq + 1:
        raise DomainError("out_of_order", f"Send batch {row.upload_seq + 1} of this keyword next (got {req.seq}).", 409)

    state = _state(db, run, settings, llm)
    run.heartbeat_at = now
    if row.status == "pending":
        row.status = "running"
    before = (row.posts_new, row.posts_dupe)
    link_refs: list[str] = []
    if req.posts:
        state.begin_keyword()
        run.current_keyword = clip(row.keyword_text, 100)
        state.step(f"Checking {len(req.posts)} posts for '{row.keyword_text}'")
        keyword_id = state._live_keyword_id(row)  # noqa: SLF001
        link_ids, refs, _ = state.ingest_batch(row, [_scraped(x) for x in req.posts], keyword_id, triage=state.fatal_llm is None)
        link_refs = [refs[sid] for sid in sorted(link_ids) if sid in refs]
    if state.fatal_llm and not run.error_message:
        run.error_message = clip(state.fatal_llm, 500)  # remembered: later batches skip triage, complete() fails the run
    row = db.get(ScrapeRunKeyword, row_id) or row
    run = db.get(ScrapeRun, run_id) or run

    stop_kind: str = "llm" if state.fatal_llm else "none"
    if req.final:
        meta = req.result or p.KeywordResultMeta()
        _close_keyword(db, state, run, row, meta)
        if stop_kind == "none":
            stop_kind = _stop_after_keyword(db, state, run, row, settings)
    row.upload_seq = req.seq
    db.commit()
    return p.KeywordAck(
        accepted=len(req.posts),
        new=row.posts_new - before[0],
        dupes=row.posts_dupe - before[1],
        link_refs=link_refs,
        continue_run=stop_kind == "none",
        stop_kind=stop_kind,  # type: ignore[arg-type]
        caps=caps_for(db, run, settings),
    )


def _close_keyword(db: Session, state: Any, run: ScrapeRun, row: ScrapeRunKeyword, meta: p.KeywordResultMeta) -> None:
    result = _result_of(row, meta)
    searcher = SimpleNamespace(page_loads=meta.page_loads_total)
    started = time.monotonic() - (meta.duration_ms or 0) / 1000
    if meta.scan_error:  # the scan itself failed (ScrapeError): same bookkeeping as the local job
        state.note_page_loads(searcher, None)
        row.status = "error"
        row.errors += 1
        row.error_message = clip(meta.scan_error, 300)
        row.duration_ms = int((time.monotonic() - started) * 1000)
        run.errors_count += 1
        run.keywords_done += 1
        state._mark_keyword_run(row)  # noqa: SLF001
        db.commit()
        state._log_keyword(row, "error", None)  # noqa: SLF001
        return
    state.note_page_loads(searcher, result)
    row.scrolls = meta.scrolls
    state.finish_keyword(row, result, state._live_keyword_id(row), started)  # noqa: SLF001


def _stop_after_keyword(db: Session, state: Any, run: ScrapeRun, row: ScrapeRunKeyword, settings: Settings) -> str:
    """The local job's between-keyword checks: daily post cap, then daily page-load cap (only while keywords remain)."""
    remaining = db.scalar(
        select(func.count(ScrapeRunKeyword.id)).where(
            ScrapeRunKeyword.run_id == run.id, ScrapeRunKeyword.status.in_(("pending", "running")), ScrapeRunKeyword.id != row.id
        )
    )
    if not remaining:
        return "none"
    if runs_service.posts_seen_today(db, account_id=run.linkedin_account_id) >= settings.scrape_max_posts_per_day:
        return "post"
    if state.page_cap_reached():
        return "page"
    return "none"


def upload_links(db: Session, run_id: int, row_id: int, req: p.UploadLinksRequest, *, now: datetime | None = None) -> p.UploadLinksResponse:
    run = _running(db, run_id)
    row = db.get(ScrapeRunKeyword, row_id)
    if row is None or row.run_id != run_id:
        raise not_found()
    run.heartbeat_at = now_or(now)
    from sales0.scraper.urls import urn_from_href

    found = 0
    for ref, url in req.links.items():
        sid = db.scalar(select(SeenPost.id).where(SeenPost.source_ref == ref))
        if sid is None:
            continue
        urn = ref if ref.startswith("urn:li:") else urn_from_href(url)
        try:
            with db.begin_nested():
                if seen_posts.attach_permalink(db, sid, url, urn=urn):
                    found += 1
        except Exception:  # noqa: BLE001 - one bad link must not lose the others
            log.exception("attach_permalink_failed", seen_post_id=sid)
    row.permalinks_found += found
    run.permalinks_found += found
    db.commit()
    return p.UploadLinksResponse(attached=found)


# ---------------------------------------------------------------------- complete
def complete(db: Session, run_id: int, req: p.CompleteRequest, *, llm: LLMClient | None = None, now: datetime | None = None) -> p.CompleteResponse:
    run = db.get(ScrapeRun, run_id)
    if run is None:
        raise not_found()
    if run.status not in ("running", "queued"):  # a retry after the answer was lost
        return p.CompleteResponse(status=run.status, summary=run.current_step)
    settings = get_settings()
    state = _state(db, run, settings, llm)
    account_id = run.linkedin_account_id
    if req.page_loads is not None:
        run.page_loads = max(run.page_loads, req.page_loads)
    rows = list(db.scalars(select(ScrapeRunKeyword).where(ScrapeRunKeyword.run_id == run_id).order_by(ScrapeRunKeyword.id)).all())
    if req.row_id is not None:  # the keyword being searched when LinkedIn stopped it ends 'error', planned last next time
        stopped = db.get(ScrapeRunKeyword, req.row_id)
        if stopped is not None and stopped.run_id == run_id and stopped.status in ("pending", "running"):
            stopped.status = "error"
            state._mark_keyword_run(stopped, at=utcnow())  # noqa: SLF001
    db.commit()

    fatal = state.fatal_llm
    # The run message doubled as the fatal-AI marker while running; it is rewritten below by the outcome.
    if fatal and req.outcome not in ("safety_stop",):
        run.error_message = None
    kwargs: dict[str, Any] = {}
    if req.outcome == "safety_stop":
        kwargs.update(abort_kind=req.kind or "rate_limited", abort_url=req.url)
    elif req.outcome == "session_missing":
        kwargs.update(session_missing=True)
    elif req.outcome == "open_failed":
        kwargs.update(open_error=clip(req.error or "Could not open LinkedIn", 500))
    elif req.outcome == "cap_hit":
        kwargs.update(cap_hit=True, cap_kind=req.cap_kind or "post")
    elif req.outcome in ("crashed", "cancelled"):
        message = (
            "Stopped by an admin." if req.outcome == "cancelled" else clip(req.error or "The laptop worker failed unexpectedly", 500)
        )
        status = "aborted" if req.outcome == "cancelled" else "failed"
        _end_plain(db, run, rows, status, message)
        return p.CompleteResponse(status=run.status, summary=run.current_step)
    state.fatal_llm = fatal
    discovery_job.finalise_run(db, state, rows, account_id, settings, **kwargs)
    run = db.get(ScrapeRun, run_id) or run
    if run.status == "succeeded":
        linkedin_accounts.mark_connected(db, account_id)  # the worker just used the session successfully
        db.commit()
    return p.CompleteResponse(status=run.status, summary=run.current_step)


def _end_plain(db: Session, run: ScrapeRun, rows: list[ScrapeRunKeyword], status: str, message: str) -> None:
    now = utcnow()
    record_history(db, "scrape_run", run.id, "status", run.status, status, note=message[:200], now=now)
    run.status, run.error_message, run.finished_at, run.current_keyword = status, message[:500], now, None
    run.current_step = ("Run stopped - " if status == "aborted" else "Run failed - ") + discovery_job.summary_text(run)
    for row in rows:
        if row.status in ("pending", "running"):
            row.status = "skipped"
            row.error_message = "Not searched - run ended"[:300]
    runs_service.sync_account_state(db, run.linkedin_account_id, now)
    db.commit()


__all__ = [
    "OFFLINE_HINT", "WorkerStatusView", "cancel_run", "claim", "complete", "heartbeat", "reap_stale",
    "upload_links", "upload_posts", "worker_status",
]
