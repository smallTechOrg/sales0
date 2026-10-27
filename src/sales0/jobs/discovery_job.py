"""The discovery run (architecture.md -> Discovery run data flow).

Runs on the JobRunner thread with its own DB session, committing after every post so
``/runs/{id}/progress`` polling sees live counters. Flow per run:

1. queued -> running; pick active keywords (never-run first, then oldest ``last_run_at``,
   then id), capped at SALES0_SCRAPE_MAX_KEYWORDS_PER_RUN; one ``scrape_run_keywords`` row each.
2. Open the persistent browser (``LinkedInSearcher``); entering verifies the session.
3. Per keyword: search -> ``dedupe.upsert_post`` per post -> keyword log row -> triage the
   keyword's NEW enquiries -> daily post cap check -> paced pause.
4. ``AccountSafetyStop``: browser closed at once, run ``aborted``, session
   ``needs_attention``, remaining keywords ``skipped``; already-saved enquiries still get
   triaged (no LinkedIn request involved).
5. Fatal LLM error (bad key / workspace / model): triage stops, remaining keywords are
   skipped, the run ends ``failed`` with the mapped message; scraped enquiries stay
   ``pending`` (Retry triage in the inbox once the key is fixed).

Phase 1.1:
- ``SessionMissing`` (no ``li_at`` in the profile; raised before any LinkedIn request):
  the run ends ``failed`` "Not logged in …", keywords ``skipped``, session
  ``not_connected``; ``abort_reason`` stays null, so no cool-down and no banner.
- The keyword whose search tripped an ``AccountSafetyStop`` gets ``last_run_at = now``
  so the next run plans it last. A stop at the run-start check stamps nothing.
- Keyword status/message come from ``services.runs.keyword_outcome`` (drift and field
  misses are a ``warning``; nothing is silent). ``scrape_keyword`` / ``scrape_run`` log
  the Phase 1.1 fields (``cards_found``, ``field_misses``, ``drift``, ``layout_warnings``,
  ``browser``).

Phase 1.2: ``scrape_keyword`` also logs ``strategy`` (``sdui|urn|none``), ``permalink_attempts``
and ``permalinks_found``; posts are identified by ``source_ref`` (a URN or ``ck:<KEY>``).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sales0.clock import utcnow
from sales0.config import Settings, get_settings
from sales0.db.engine import get_sessionmaker
from sales0.db.models import Enquiry, Keyword, ScrapeRun, ScrapeRunKeyword
from sales0.llm import LLMClient
from sales0.logging import bind_context, get_logger, unbind_context
from sales0.jobs.session_log import log_run_start
from sales0.scraper.errors import AccountSafetyStop, ScrapeError, SessionMissing
from sales0.services import dedupe
from sales0.services import session as session_service
from sales0.services._common import clip, record_history
from sales0.services.runs import keyword_outcome, posts_seen_today
from sales0.services.triage import FATAL_LLM_ERRORS, triage_enquiry

log = get_logger("sales0.jobs.discovery")

SearcherFactory = Callable[[Settings], Any]

SAFETY_LABELS = {
    "logout": "LinkedIn session logged out",
    "checkpoint": "LinkedIn checkpoint detected",
    "captcha": "LinkedIn captcha detected",
    "rate_limited": "LinkedIn rate limit detected",
}


NOT_LOGGED_IN_SKIP = "Not searched — not logged in"


class _FatalLLM(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _default_searcher_factory(settings: Settings) -> Any:
    from sales0.scraper.search import LinkedInSearcher

    return LinkedInSearcher(settings)


def select_keywords(db: Session, limit: int) -> list[tuple[int, str]]:
    """Active keywords as ``(id, text)``, never-run first, then least-recently run, then id.

    Plain tuples, not ORM objects: a keyword can be deleted on Setup while the run is in
    progress, and a stale identity-mapped ``Keyword`` would then fail the run's flush.
    """
    never_run_first = case((Keyword.last_run_at.is_(None), 0), else_=1)
    rows = db.execute(
        select(Keyword.id, Keyword.text)
        .where(Keyword.active.is_(True))
        .order_by(never_run_first, Keyword.last_run_at, Keyword.id)
        .limit(limit)
    ).all()
    return [(int(kid), str(text)) for kid, text in rows]


class _Run:
    """State + helpers for one execution (keeps ``execute`` readable)."""

    def __init__(
        self,
        db: Session,
        run_id: int,
        settings: Settings,
        llm: LLMClient | None,
        sleep: Callable[[float], None],
        rng: random.Random,
    ) -> None:
        self.db = db
        self.run_id = run_id
        self.settings = settings
        self.llm = llm
        self.sleep = sleep
        self.rng = rng
        self.fatal_llm: str | None = None
        self.pending_triage: list[int] = []  # NEW enquiries of this run not yet triaged

    @property
    def run(self) -> ScrapeRun:
        run = self.db.get(ScrapeRun, self.run_id)
        assert run is not None
        return run

    def step(self, text: str, keyword: str | None = None, *, keep_keyword: bool = True) -> None:
        run = self.run
        run.current_step = clip(text, 200)
        if keyword is not None or not keep_keyword:
            run.current_keyword = clip(keyword, 100)
        self.db.commit()

    def set_status(self, status: str, *, note: str | None = None) -> None:
        run = self.run
        if run.status != status:
            record_history(self.db, "scrape_run", run.id, "status", run.status, status, note=note)
            run.status = status

    # ------------------------------------------------------------------ triage
    def triage_pending(self) -> None:
        total = len(self.pending_triage)
        done = 0
        while self.pending_triage and self.fatal_llm is None:
            enquiry_id = self.pending_triage[0]
            done += 1
            self.step(f"Triaging {total} new enquir{'y' if total == 1 else 'ies'} ({done}/{total})")
            bind_context(enquiry_id=enquiry_id)
            try:
                enquiry = self.db.get(Enquiry, enquiry_id)
                if enquiry is None or enquiry.triage_status != "pending":
                    self.pending_triage.pop(0)
                    continue
                outcome = triage_enquiry(self.db, enquiry_id, self.llm, run_id=self.run_id)
            except FATAL_LLM_ERRORS as exc:
                self.fatal_llm = getattr(exc, "message", str(exc))
                self.db.commit()  # keep the llm_calls error row
                log.error("triage_fatal", error_type=getattr(exc, "error_type", type(exc).__name__))
                break
            except Exception as exc:  # noqa: BLE001 — one bad enquiry must not kill the run
                self.db.rollback()
                log.exception("triage_crashed", error_type=type(exc).__name__)
                enquiry = self.db.get(Enquiry, enquiry_id)
                if enquiry is not None and enquiry.triage_status == "pending":
                    enquiry.triage_status = "failed"
                    enquiry.triage_error = clip(f"AI triage crashed ({type(exc).__name__}) — Retry triage", 300)
                self.run.triage_failed += 1
                self.pending_triage.pop(0)
                self.db.commit()
                continue
            finally:
                unbind_context("enquiry_id")
            self.pending_triage.pop(0)
            run = self.run
            if outcome.status == "done":
                run.enquiries_triaged += 1
            else:
                run.triage_failed += 1
            self.db.commit()

    # ------------------------------------------------------------------ keywords
    def skip_remaining(self, rows: list[ScrapeRunKeyword], message: str | None = None) -> None:
        for row in rows:
            if row.status in ("pending", "running"):
                row.status = "skipped"
                if message:
                    row.error_message = clip(message, 300)
        self.db.commit()

    def scrape_keyword(self, searcher: Any, row: ScrapeRunKeyword, index: int, total: int) -> None:
        run = self.run
        started = time.monotonic()
        row.status = "running"
        self.step(f"Searching '{row.keyword_text}' ({index}/{total})", row.keyword_text)
        remaining_today = self.settings.scrape_max_posts_per_day - posts_seen_today(self.db)
        max_posts = max(1, min(self.settings.scrape_max_posts_per_keyword, remaining_today))
        try:
            result = searcher.search(row.keyword_text, max_posts)
        except AccountSafetyStop:
            row.status = "error"
            row.duration_ms = int((time.monotonic() - started) * 1000)
            # The keyword that tripped LinkedIn counts as "just run": the next run plans it
            # last instead of opening with the same search (data flow step 6).
            self._mark_keyword_run(row, at=utcnow())
            self.db.commit()
            raise
        except ScrapeError as exc:
            row.status = "error"
            row.errors += 1
            row.error_message = clip(f"Page error: {exc}", 300)
            row.duration_ms = int((time.monotonic() - started) * 1000)
            run.errors_count += 1
            run.keywords_done += 1
            self._mark_keyword_run(row)
            self.db.commit()
            self._log_keyword(row, "error", None)
            return

        self.step(f"Saving {len(result.posts)} posts for '{row.keyword_text}' ({index}/{total})")
        # The keyword may have been deleted on Setup while it was being searched: the hit
        # then keeps only its text snapshot (keyword_id NULL) instead of failing the FK.
        keyword_id = self._live_keyword_id(row)
        for post in result.posts:
            try:
                try:
                    kind, enquiry_id = dedupe.upsert_post(self.db, post, self.run_id, keyword_id, row.keyword_text)
                except IntegrityError:
                    if keyword_id is None:
                        raise
                    # Race: the keyword was deleted on Setup between posts, so the hit's FK
                    # fails. Earlier posts are committed; retry this one once as a
                    # text-only hit, and save the rest of this keyword's posts the same way.
                    self.db.rollback()
                    log.warning("keyword_deleted_mid_save", keyword=row.keyword_text, ref=getattr(post, "source_ref", None))
                    keyword_id = None
                    row = self.db.get(ScrapeRunKeyword, row.id) or row
                    kind, enquiry_id = dedupe.upsert_post(self.db, post, self.run_id, None, row.keyword_text)
            except Exception as exc:  # noqa: BLE001 — a malformed post must not lose the others
                self.db.rollback()
                log.exception("post_save_failed", ref=getattr(post, "source_ref", None), error_type=type(exc).__name__)
                row = self.db.get(ScrapeRunKeyword, row.id) or row
                row.errors += 1
                self.run.errors_count += 1
                self.db.commit()
                continue
            run = self.run
            row.posts_seen += 1
            run.posts_seen += 1
            if kind == "new":
                row.posts_new += 1
                run.posts_new += 1
                self.pending_triage.append(enquiry_id)
            else:
                row.posts_dupe += 1
                run.posts_dupe += 1
            self.db.commit()

        if result.parse_errors:
            row.errors += int(result.parse_errors)
            self.run.errors_count += int(result.parse_errors)
        status, message = keyword_outcome(result)
        if message:
            row.error_message = clip(message, 300)
        row.status = status
        row.duration_ms = int((time.monotonic() - started) * 1000)
        self.run.keywords_done += 1
        self._mark_keyword_run(row)
        self.db.commit()
        self._log_keyword(row, status, result)

    def _live_keyword_id(self, row: ScrapeRunKeyword) -> int | None:
        if row.keyword_id is None:
            return None
        found = self.db.scalar(select(Keyword.id).where(Keyword.id == row.keyword_id))
        return int(found) if found is not None else None

    def _mark_keyword_run(self, row: ScrapeRunKeyword, *, at: datetime | None = None) -> None:
        # Core UPDATE: a keyword deleted mid-run simply matches 0 rows.
        if row.keyword_id is not None:
            self.db.execute(
                update(Keyword)
                .where(Keyword.id == row.keyword_id)
                .values(last_run_at=at or utcnow())
                .execution_options(synchronize_session=False)
            )

    def _log_keyword(self, row: ScrapeRunKeyword, status: str, result: Any | None) -> None:
        emit = log.warning if status == "warning" else log.info
        emit(
            "scrape_keyword",
            keyword=row.keyword_text,
            posts_seen=row.posts_seen,
            posts_new=row.posts_new,
            posts_dupe=row.posts_dupe,
            errors=row.errors,
            duration_ms=row.duration_ms,
            status=status,
            cards_found=int(getattr(result, "cards_found", 0) or 0),
            parse_errors=int(getattr(result, "parse_errors", 0) or 0),
            field_misses=dict(getattr(result, "field_misses", None) or {}),
            drift=bool(getattr(result, "drift_warning", False)),
            no_results=bool(getattr(result, "no_results", False)),
            strategy=str(getattr(result, "strategy", None) or "none"),
            permalink_attempts=int(getattr(result, "permalink_attempts", 0) or 0),
            permalinks_found=int(getattr(result, "permalinks_found", 0) or 0),
        )


def execute(
    run_id: int,
    *,
    llm: LLMClient | None = None,
    searcher_factory: SearcherFactory | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> None:
    """Run discovery for a queued run. Never raises: every outcome is written to the run row."""
    settings = get_settings()
    factory = searcher_factory or _default_searcher_factory
    bind_context(run_id=run_id, job="run")
    started = time.monotonic()
    db = get_sessionmaker()()
    try:
        _execute(db, run_id, settings, llm, factory, sleep, rng or random.Random())
    except Exception as exc:  # noqa: BLE001 — last-resort: the run must never stay "running"
        log.exception("scrape_run_crashed", error_type=type(exc).__name__)
        db.rollback()
        _finalise_crash(db, run_id, f"Run failed unexpectedly ({type(exc).__name__}) — see the server log")
    finally:
        run = db.get(ScrapeRun, run_id)
        if run is not None:
            log.info(
                "scrape_run",
                status=run.status,
                keywords_planned=run.keywords_planned,
                keywords_done=run.keywords_done,
                posts_seen=run.posts_seen,
                posts_new=run.posts_new,
                posts_dupe=run.posts_dupe,
                enquiries_triaged=run.enquiries_triaged,
                triage_failed=run.triage_failed,
                errors_count=run.errors_count,
                abort_reason=run.abort_reason,
                duration_ms=int((time.monotonic() - started) * 1000),
                layout_warnings=_layout_warnings(db, run_id),
                browser=session_service.browser_channel(),
            )
        db.close()
        unbind_context("run_id")


def _layout_warnings(db: Session, run_id: int) -> int:
    return int(
        db.scalar(
            select(func.count(ScrapeRunKeyword.id)).where(
                ScrapeRunKeyword.run_id == run_id, ScrapeRunKeyword.status == "warning"
            )
        )
        or 0
    )


def _finalise_crash(db: Session, run_id: int, message: str) -> None:
    run = db.get(ScrapeRun, run_id)
    if run is None or run.status not in ("queued", "running"):
        return
    record_history(db, "scrape_run", run.id, "status", run.status, "failed", note=message)
    run.status = "failed"
    run.error_message = clip(message, 500)
    run.finished_at = utcnow()
    run.current_step = "Run failed"
    for row in db.scalars(select(ScrapeRunKeyword).where(ScrapeRunKeyword.run_id == run_id)).all():
        if row.status in ("pending", "running"):
            row.status = "skipped"
    db.commit()


def _execute(
    db: Session,
    run_id: int,
    settings: Settings,
    llm: LLMClient | None,
    factory: SearcherFactory,
    sleep: Callable[[float], None],
    rng: random.Random,
) -> None:
    state = _Run(db, run_id, settings, llm, sleep, rng)
    run = db.get(ScrapeRun, run_id)
    if run is None or run.status != "queued":
        log.warning("scrape_run_not_queued", status=getattr(run, "status", None))
        return

    keywords = select_keywords(db, settings.scrape_max_keywords_per_run)
    rows = [
        ScrapeRunKeyword(run_id=run_id, keyword_id=kid, keyword_text=text, status="pending") for kid, text in keywords
    ]
    db.add_all(rows)
    run.keywords_planned = len(rows)
    state.set_status("running")
    run.current_step = "Opening LinkedIn and checking the session"
    db.commit()

    if not rows:
        run.status = "failed"
        run.error_message = "Add at least one active keyword in Setup."
        run.finished_at = utcnow()
        db.commit()
        return

    abort_kind: str | None = None
    abort_url: str | None = None
    open_error: str | None = None
    session_missing = False
    entered = False
    cap_hit = False
    try:
        with factory(settings) as searcher:
            entered = True
            check = getattr(searcher, "session", None)
            log_run_start("connected", "ok", url=getattr(check, "url", None), cookie_present=True,
                          soft_hints=tuple(getattr(check, "soft_hints", ()) or ()))
            for index, row in enumerate(rows, start=1):
                if index > 1:
                    seconds = rng.uniform(settings.scrape_keyword_pause_min_s, settings.scrape_keyword_pause_max_s)
                    state.step(f"Pausing {round(seconds)}s before next keyword")
                    sleep(seconds)
                state.scrape_keyword(searcher, row, index, len(rows))
                state.triage_pending()
                if state.fatal_llm is not None:
                    break
                if posts_seen_today(db) >= settings.scrape_max_posts_per_day and index < len(rows):
                    cap_hit = True
                    break
    except SessionMissing as missing:
        db.rollback()
        session_missing = True
        log_run_start("not_connected", "no_cookie", url=getattr(missing, "url", None), cookie_present=False)
    except AccountSafetyStop as stop:
        db.rollback()
        abort_kind, abort_url = stop.kind, stop.url
        if not entered:
            log_run_start("needs_attention", stop.kind, url=stop.url, cookie_present=True)
        log.warning("scrape_safety_stop", kind=stop.kind, url=stop.url, at_run_start=not entered)
    except ScrapeError as exc:  # only the session check on open raises it outside a keyword
        db.rollback()
        open_error = clip(f"Could not open LinkedIn: {exc}", 500)
        log.error("scrape_open_failed", error=str(exc)[:200])

    run = state.run
    now = utcnow()
    if abort_kind is not None:
        message = session_service.mark_needs_attention(db, abort_kind, abort_url)
        for row in rows:
            row_db = db.get(ScrapeRunKeyword, row.id)
            if row_db is not None and row_db.status == "error" and row_db.error_message is None:
                row_db.error_message = clip(f"Stopped: {SAFETY_LABELS.get(abort_kind, abort_kind)}", 300)
                row_db.errors += 1
                run.errors_count += 1
        state.skip_remaining(rows, f"Not searched — run stopped ({abort_kind})")
        # Saved-but-untriaged enquiries are still triaged: no LinkedIn request involved.
        state.triage_pending()
        run = state.run
        run.abort_reason = abort_kind
        label = SAFETY_LABELS.get(abort_kind, abort_kind)
        if state.fatal_llm is not None:
            # Status stays aborted (the safety stop came first) but the AI failure must show too.
            stopped = f"Run stopped: {label}. AI triage also stopped: {state.fatal_llm.rstrip('.')}. {message}"
        else:
            stopped = f"Run stopped: {label}. {message}"
        run.error_message = clip(stopped, 500)
        state.set_status("aborted", note=abort_kind)
        run.current_step = f"Run stopped: {SAFETY_LABELS.get(abort_kind, abort_kind)}"
    elif session_missing:
        message = session_service.NOT_LOGGED_IN_MESSAGE
        session_service.mark_not_connected(db, message)
        state.skip_remaining(rows, NOT_LOGGED_IN_SKIP)
        run = state.run
        run.error_message = message
        state.set_status("failed", note="not_logged_in")
        run.current_step = "Run failed — not logged in to LinkedIn in sales0's browser"
    elif open_error is not None:
        state.skip_remaining(rows, "Not searched — LinkedIn could not be opened")
        run = state.run
        run.error_message = open_error
        run.errors_count += 1
        state.set_status("failed", note="open_failed")
        run.current_step = "Run failed — LinkedIn could not be opened"
    elif state.fatal_llm is not None:
        state.skip_remaining(rows, "Not searched — AI triage stopped")
        run = state.run
        run.error_message = clip(state.fatal_llm, 500)
        state.set_status("failed", note="llm_fatal")
        run.current_step = "AI triage stopped — fix the Anthropic settings in .env, then Retry triage"
    else:
        if cap_hit:
            posts_today = posts_seen_today(db)
            note = f"Daily post limit reached ({posts_today}/{settings.scrape_max_posts_per_day})"
            state.skip_remaining(rows, "Not searched — daily post limit reached")
            run = state.run
            run.abort_reason = "daily_post_cap"
            run.current_step = f"{note} — remaining keywords skipped"
        else:
            run.current_step = f"Run finished — {run.posts_new} new enquir{'y' if run.posts_new == 1 else 'ies'}"
        state.set_status("succeeded")
        session_service.mark_successful_run(db, now=now)
    run.current_keyword = None
    run.finished_at = now
    db.commit()


__all__ = ["execute", "select_keywords"]
