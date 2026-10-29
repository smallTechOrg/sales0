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

Phase 1.4 (architecture.md -> Thorough discovery): purge on run start; ``plan_keywords``;
per keyword ``scan`` -> ``register_sighting`` (commit every 10) -> concurrent triage of the
unseen posts only (``persist_triage`` on this thread) -> ``capture_links`` for the genuine
posts -> ``attach_permalink`` -> ``record_yield``; one commit per keyword. The summary states
scanned vs relevant honestly. A restart marks the run ``aborted`` (services.runs) and the next
``plan_keywords`` resumes with the un-run keywords.

Phase 1.2: ``scrape_keyword`` also logs ``strategy`` (``sdui|urn|none``), ``permalink_attempts``
and ``permalinks_found``; posts are identified by ``source_ref`` (a URN or ``ck:<KEY>``).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sales0.clock import utcnow
from sales0.config import Settings, get_settings
from sales0.db.engine import get_sessionmaker
from sales0.agent import TriageDeps, load_candidates, select_examples
from sales0.agent.batch import BatchItem, triage_concurrently
from sales0.services.costs_report import SpendCapReached, ensure_within_spend_cap
from sales0.db.models import Keyword, LlmCall, ScrapeRun, ScrapeRunKeyword
from sales0.llm import LLMClient, get_llm_client
from sales0.logging import bind_context, get_logger, unbind_context
from sales0.jobs.session_log import log_run_start
from sales0.scraper.urls import urn_from_href
from sales0.scraper.errors import AccountSafetyStop, ScrapeError, SessionMissing
from sales0.services import criteria as criteria_service
from sales0.services import keywords as keywords_service
from sales0.services import seen_posts
from sales0.services import linkedin_accounts
from sales0.services import runs as runs_service
from sales0.services import session as session_service
from sales0.services._common import clip, record_history
from sales0.services.runs import keyword_outcome, page_loads_today, plan_keywords, posts_seen_today
from sales0.services.triage import active_offering_refs

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


def _default_searcher_factory(
    settings: Settings, account_id: int | None = None, page_load_budget: int | None = None
) -> Any:
    """A ``LinkedInSearcher`` on the run's LinkedIn account (None = the default account).

    ``page_load_budget``: the account's page loads left today (fixscrape D #8); the searcher stops its
    scans with stop_reason ``page_budget`` when it is used up."""
    from sales0.scraper.search import LinkedInSearcher
    from sales0.services import linkedin_accounts

    return LinkedInSearcher(
        settings, account=linkedin_accounts.build_handle(account_id), page_load_budget=page_load_budget
    )


def select_keywords(db: Session, limit: int) -> list[tuple[int, str]]:
    """Active keywords as ``(id, text)`` in plan order (resume-first, never-run, least-recent).

    Kept as the job's name for ``services.runs.plan_keywords``. Plain tuples, not ORM
    objects: a keyword can be deleted on Setup while the run is in progress.
    """
    return plan_keywords(db, limit)


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
        account_id: int | None = None,
    ) -> None:
        self.db = db
        self.run_id = run_id
        self.account_id = account_id  # the LinkedIn account this run uses (caps and safety are per account)
        self.settings = settings
        self.llm = llm
        self.sleep = sleep
        self.rng = rng
        self.fatal_llm: str | None = None
        self.link_attempts = 0  # "Copy link to post" attempts of the current keyword
        # seen_post id -> (keyword row id, 'relevant'|'skipped'|'failed') already counted in this run: a
        # text upgrade can re-triage the same post later in the run, which must replace, not add.
        self._counted: dict[int, tuple[int, str]] = {}
        self.card_link_kind: dict[int, str] = {}  # seen_post id -> link kind of its card (this keyword)
        self.page_budget_hit = False  # a scan ended with stop_reason 'page_budget' (daily page-load cap)

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

    # ------------------------------------------------------------------ keywords
    def skip_remaining(self, rows: list[ScrapeRunKeyword], message: str | None = None) -> None:
        for row in rows:
            if row.status in ("pending", "running"):
                row.status = "skipped"
                if message:
                    row.error_message = clip(message, 300)
        self.db.commit()

    def _register(self, post: Any, row: ScrapeRunKeyword, keyword_id: int | None) -> tuple[Any, int | None]:
        """``register_sighting`` inside a savepoint; a deleted keyword retries as text-only."""
        for attempt_id in ((keyword_id, None) if keyword_id is not None else (None,)):
            try:
                with self.db.begin_nested():
                    sighting = seen_posts.register_sighting(
                        self.db, post, self.run_id, attempt_id, row.keyword_text
                    )
                return sighting, attempt_id
            except IntegrityError:
                if attempt_id is None:
                    raise
                log.warning("keyword_deleted_mid_save", keyword=row.keyword_text, ref=getattr(post, "source_ref", None))
        raise AssertionError("unreachable")  # pragma: no cover

    def note_page_loads(self, searcher: Any, result: Any | None, *, at_least: int = 0) -> None:
        """Record the LinkedIn page loads this run used (``scrape_runs.page_loads``; feeds the daily cap).

        The searcher's cumulative ``page_loads`` is authoritative; without it the result's own count (or
        ``at_least``) is added."""
        total = getattr(searcher, "page_loads", None)
        if isinstance(total, int) and not isinstance(total, bool):
            self.run.page_loads = max(self.run.page_loads, total)
        else:
            self.run.page_loads += max(int(getattr(result, "page_loads", 0) or 0), at_least)
        if getattr(result, "stop_reason", None) == "page_budget":
            self.page_budget_hit = True

    def page_cap_reached(self) -> bool:
        """The account's daily page-load cap is used up (including this run's loads)."""
        self.db.flush()
        return (
            self.page_budget_hit
            or page_loads_today(self.db, account_id=self.account_id) >= self.settings.scrape_max_pageloads_per_day
        )

    def scrape_keyword(self, searcher: Any, row: ScrapeRunKeyword, index: int, total: int) -> None:
        run = self.run
        started = time.monotonic()
        row.status = "running"
        self.step(f"Searching '{row.keyword_text}' ({index}/{total})", row.keyword_text)
        remaining_today = self.settings.scrape_max_posts_per_day - posts_seen_today(self.db, account_id=self.account_id)
        max_posts = max(1, min(self.settings.scrape_max_posts_per_keyword, remaining_today))
        try:
            result = searcher.scan(row.keyword_text, max_posts)
        except AccountSafetyStop:
            self.note_page_loads(searcher, None)
            row.status = "error"
            row.duration_ms = int((time.monotonic() - started) * 1000)
            # The keyword that tripped LinkedIn counts as "just run": the next run plans it
            # last instead of opening with the same search (data flow step 6).
            self._mark_keyword_run(row, at=utcnow())
            self.db.commit()
            raise
        except ScrapeError as exc:
            self.note_page_loads(searcher, None)
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

        self.note_page_loads(searcher, result)
        row.scrolls = int(getattr(result, "scrolls", 0) or 0)
        total_posts = len(result.posts)
        self.step(f"Checking {total_posts} posts for '{row.keyword_text}' ({index}/{total})")
        keyword_id = self._live_keyword_id(row)
        self.begin_keyword()
        link_ids, refs, keyword_id = self.ingest_batch(row, result.posts, keyword_id)

        if self.fatal_llm is None and link_ids:
            try:
                self.capture_links(searcher, row, link_ids, refs, index, total)
            except AccountSafetyStop:
                # Everything scanned and triaged so far stays; the yield is still recorded.
                keywords_service.record_yield(self.db, keyword_id, row.posts_seen, row.posts_relevant)
                row.duration_ms = int((time.monotonic() - started) * 1000)
                self.db.commit()
                raise

        self.finish_keyword(row, result, keyword_id, started)

    # ------------------------------------------------------------------ ingest / finish (shared with the worker API)
    def begin_keyword(self) -> None:
        """Per-keyword scratch state (permalink attempts, the cards' link kinds)."""
        self.link_attempts = 0
        self.card_link_kind = {}

    def ingest_batch(
        self, row: ScrapeRunKeyword, posts: Any, keyword_id: int | None, *, triage: bool = True
    ) -> tuple[set[int], dict[int, str], int | None]:
        """Register a batch of scraped posts (dedupe), count them, triage the unseen ones.

        Returns ``(seen_post ids that want a permalink, seen_post id -> source_ref, keyword_id)``. The keyword may
        have been deleted on Setup while it was being searched: the hit then keeps only its text snapshot
        (keyword_id NULL) instead of failing the FK. ``triage=False`` (hosted: a fatal AI error already stopped the
        run) registers the posts but leaves them ``pending`` for a later "Retry triage"."""
        to_triage: list[int] = []  # seen_post ids needing a verdict (new, or pending from a stopped run)
        refs: dict[int, str] = {}  # seen_post id -> source_ref (for permalink capture)
        link_ids: set[int] = set()  # seen_post ids that want a permalink
        since_commit = 0
        for post in posts:
            try:
                sighting, keyword_id = self._register(post, row, keyword_id)
            except Exception as exc:  # noqa: BLE001 — a malformed post must not lose the others
                log.exception("post_save_failed", ref=getattr(post, "source_ref", None), error_type=type(exc).__name__)
                row.errors += 1
                self.run.errors_count += 1
                self.db.commit()
                continue
            run = self.run
            row.posts_seen += 1
            run.posts_seen += 1
            if sighting.kind == "new":
                row.posts_new += 1
                run.posts_new += 1
            else:
                row.posts_dupe += 1
                run.posts_dupe += 1
            if sighting.kind == "new" or sighting.triage_status == "pending":
                if sighting.seen_post_id not in refs:
                    to_triage.append(sighting.seen_post_id)
            if sighting.needs_permalink:
                link_ids.add(sighting.seen_post_id)
            refs.setdefault(sighting.seen_post_id, post.source_ref)
            self.card_link_kind[sighting.seen_post_id] = str(getattr(post, "link_kind", "post"))
            since_commit += 1
            if since_commit >= 10:
                self.db.commit()
                since_commit = 0
        self.db.commit()

        if triage:
            self.triage_batch(row, to_triage, keyword_id, link_ids)
        return link_ids, refs, keyword_id

    def finish_keyword(self, row: ScrapeRunKeyword, result: Any, keyword_id: int | None, started: float) -> None:
        """Status, message, yield and counters of a keyword whose posts are all in."""
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
        keywords_service.record_yield(self.db, keyword_id, row.posts_seen, row.posts_relevant)
        self.db.commit()
        self._log_keyword(row, status, result)

    # ------------------------------------------------------------------ triage
    def triage_batch(
        self, row: ScrapeRunKeyword, seen_post_ids: list[int], keyword_id: int | None, link_ids: set[int]
    ) -> None:
        """Triage unseen posts 4 at a time; persist each result on this thread (commit each)."""
        if not seen_post_ids:
            return
        inputs = seen_posts.triage_inputs(self.db, seen_post_ids)
        if not inputs:
            return
        settings = self.settings
        started = time.monotonic()
        client = self.llm or get_llm_client()
        deps = TriageDeps(
            llm=client,
            offerings=active_offering_refs(self.db),
            examples=select_examples(
                load_candidates(self.db, exclude_enquiry_id=0), settings.fewshot_max_examples, settings.fewshot_text_chars
            ),
            settings=settings,
            criteria=criteria_service.active_set(self.db),  # q14: the team's active include/exclude rules
        )
        self.db.commit()  # release the read snapshot before the pool starts
        items = [BatchItem(key=sid, inp=inp) for sid, inp in inputs]
        done = 0

        def over_spend_cap() -> bool:
            # Phase 4 hard stop, polled on this thread before each submission: nothing new is sent
            # once the monthly cap is used up; unsent posts stay pending (not triaged, not lost).
            try:
                ensure_within_spend_cap(self.db)
            except SpendCapReached as cap:
                self.fatal_llm = cap.message if hasattr(cap, "message") else str(cap)
                return True
            return False

        try:
            for result in triage_concurrently(
                items, deps, concurrency=settings.triage_concurrency, should_stop=over_spend_cap
            ):
                done += 1
                self.step(f"Triaging new posts for '{row.keyword_text}' ({done}/{len(items)})")
                if result.fatal is not None:
                    self.fatal_llm = getattr(result.fatal, "message", str(result.fatal))
                    self.db.add(
                        LlmCall(
                            feature="triage",
                            model=settings.llm_model,
                            prompt_version="triage-v2",
                            seen_post_id=result.key,
                            run_id=self.run_id,
                            status="error",
                            error_type=clip(getattr(result.fatal, "error_type", type(result.fatal).__name__), 100),
                        )
                    )
                    self.db.commit()
                    log.error("triage_fatal", error_type=type(result.fatal).__name__)
                    continue  # in-flight calls still finish and are persisted
                self._persist(row, result.key, result.outcome, keyword_id, link_ids)
        except Exception as exc:  # noqa: BLE001 — a crashed worker must not kill the run
            self.db.rollback()
            log.exception("triage_crashed", error_type=type(exc).__name__)
            self.run.triage_failed += 1
            row.errors += 1
            self.db.commit()
        log.info(
            "triage_batch", items=len(items), concurrency=settings.triage_concurrency,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def _persist(
        self, row: ScrapeRunKeyword, seen_post_id: int, outcome: Any, keyword_id: int | None, link_ids: set[int]
    ) -> None:
        error = getattr(outcome, "error", None) if outcome is not None and outcome.status != "done" else None
        try:
            persisted = None
            for attempt_id in ((keyword_id, None) if keyword_id is not None else (None,)):
                try:
                    with self.db.begin_nested():
                        persisted = seen_posts.persist_triage(
                            self.db, seen_post_id, outcome, error=error, run_id=self.run_id, keyword_id=attempt_id
                        )
                    break
                except IntegrityError:
                    if attempt_id is None:
                        raise
                    log.warning("keyword_deleted_mid_save", keyword=row.keyword_text, seen_post_id=seen_post_id)
            assert persisted is not None
        except Exception as exc:  # noqa: BLE001
            self.db.rollback()
            log.exception("persist_triage_failed", seen_post_id=seen_post_id, error_type=type(exc).__name__)
            row = self.db.get(ScrapeRunKeyword, row.id) or row
            self.run.triage_failed += 1
            row.errors += 1
            self.db.commit()
            return
        run = self.run
        row.cost_usd_micros += int(persisted.cost_usd_micros or 0)
        prev = self._counted.pop(seen_post_id, None)
        if prev is not None:  # re-triage after a text upgrade: undo the earlier tally first
            prev_row = self.db.get(ScrapeRunKeyword, prev[0]) or row
            if prev[1] == "relevant":
                prev_row.posts_relevant -= 1
                run.posts_relevant -= 1
            elif prev[1] == "repeat":
                run.posts_repeat_skipped -= 1
            elif prev[1] == "skipped":
                prev_row.posts_skipped_ai -= 1
                run.posts_skipped_ai -= 1
            else:
                run.triage_failed -= 1
        kind = "failed"
        if persisted.triage_status == "done":
            run.enquiries_triaged += 1
            if persisted.verdict == "genuine" and persisted.enquiry_id is not None and persisted.repeat_skipped:
                # q14: a further enquiry from an existing prospect in this run - auto-discarded, no post-link click
                kind = "repeat"
                run.posts_repeat_skipped += 1
            elif persisted.verdict == "genuine" and persisted.enquiry_id is not None:
                kind = "relevant"
                row.posts_relevant += 1
                run.posts_relevant += 1
                if self.card_link_kind.get(seen_post_id) != "post":  # a post link needs no menu click
                    link_ids.add(seen_post_id)
            else:
                kind = "skipped"
                row.posts_skipped_ai += 1
                run.posts_skipped_ai += 1
        else:
            run.triage_failed += 1
        self._counted[seen_post_id] = (row.id, kind)
        self.db.commit()

    # ------------------------------------------------------------------ permalinks
    def capture_links(
        self, searcher: Any, row: ScrapeRunKeyword, link_ids: set[int], refs: dict[int, str], index: int, total: int
    ) -> None:
        wanted = {sid: refs[sid] for sid in sorted(link_ids) if sid in refs}
        if not wanted or not hasattr(searcher, "capture_links"):
            return
        self.step(f"Copying post links for '{row.keyword_text}' ({index}/{total})")
        try:
            captured = searcher.capture_links(list(wanted.values()))
        except AccountSafetyStop:
            # Everything already persisted is kept; the handler in _execute does the rest.
            row.status = "error"
            self._mark_keyword_run(row, at=utcnow())
            self.db.commit()
            raise
        except ScrapeError as exc:
            log.warning("capture_links_failed", keyword=row.keyword_text, error=str(exc)[:200])
            return
        links: dict[str, str] = dict(getattr(captured, "links", {}) or {})
        self.link_attempts = int(getattr(captured, "attempts", 0) or 0)
        found = 0
        for sid, ref in wanted.items():
            url = links.get(ref)
            if not url:
                continue
            urn = ref if ref.startswith("urn:li:") else urn_from_href(url)
            try:
                if seen_posts.attach_permalink(self.db, sid, url, urn=urn):
                    found += 1
            except Exception as exc:  # noqa: BLE001
                self.db.rollback()
                log.exception("attach_permalink_failed", seen_post_id=sid, error_type=type(exc).__name__)
                row = self.db.get(ScrapeRunKeyword, row.id) or row
        row.permalinks_found += found
        self.run.permalinks_found += found
        self.db.commit()

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
            posts_relevant=row.posts_relevant,
            posts_skipped_ai=row.posts_skipped_ai,
            errors=row.errors,
            duration_ms=row.duration_ms,
            status=status,
            cards_found=int(getattr(result, "cards_found", 0) or 0),
            parse_errors=int(getattr(result, "parse_errors", 0) or 0),
            field_misses=dict(getattr(result, "field_misses", None) or {}),
            drift=bool(getattr(result, "drift_warning", False)),
            no_results=bool(getattr(result, "no_results", False)),
            strategy=str(getattr(result, "strategy", None) or "none"),
            permalink_attempts=self.link_attempts,
            permalinks_found=row.permalinks_found,
            scrolls=row.scrolls,
            passes=int(getattr(result, "passes", 1) or 1) if result is not None else 0,
            expanded=int(getattr(result, "expanded", 0) or 0),
            stop_reason=str(getattr(result, "stop_reason", None) or "cap"),
            cost_usd=round(row.cost_usd_micros / 1_000_000, 6),
        )


def summary_text(run: ScrapeRun) -> str:
    """The honest funnel: scanned vs new vs relevant (never "found N enquiries")."""
    pct = f" ({round(run.posts_relevant / run.posts_new * 100)}%)" if run.posts_new else ""
    repeats = int(getattr(run, "posts_repeat_skipped", 0) or 0)
    return (
        f"scanned {run.posts_seen} · new {run.posts_new} · relevant {run.posts_relevant}{pct}"
        f" · skipped by AI {run.posts_skipped_ai} · already seen {run.posts_dupe}"
        + (f" · repeats from pipeline skipped {repeats}" if repeats else "")
    )


def relevance_pct(run: ScrapeRun) -> float | None:
    return round(run.posts_relevant / run.posts_new * 100, 1) if run.posts_new else None


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
    factory = searcher_factory  # None = the real searcher on the run's account (resolved in _execute)
    bind_context(run_id=run_id, job="run")
    started = time.monotonic()
    db = get_sessionmaker()()
    info: dict[str, int] = {"purged": 0}
    try:
        _execute(db, run_id, settings, llm, factory, sleep, rng or random.Random(), info)
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
                posts_relevant=run.posts_relevant,
                posts_skipped_ai=run.posts_skipped_ai,
                permalinks_found=run.permalinks_found,
                relevance_pct=relevance_pct(run),
                purged=info['purged'],
                enquiries_triaged=run.enquiries_triaged,
                triage_failed=run.triage_failed,
                errors_count=run.errors_count,
                abort_reason=run.abort_reason,
                duration_ms=int((time.monotonic() - started) * 1000),
                layout_warnings=_layout_warnings(db, run_id),
                browser=session_service.browser_channel(),
            )
        db.close()
        unbind_context("run_id", "linkedin_account_id")


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
    factory: SearcherFactory | None,
    sleep: Callable[[float], None],
    rng: random.Random,
    info: dict[str, int],
) -> None:
    state = _Run(db, run_id, settings, llm, sleep, rng)
    run = db.get(ScrapeRun, run_id)
    if run is None or run.status != "queued":
        log.warning("scrape_run_not_queued", status=getattr(run, "status", None))
        return
    if run.linkedin_account_id is None:  # queued by older code / a test: stamp the default account
        run.linkedin_account_id = linkedin_accounts.default_account_id(db)
        db.flush()
    account_id = run.linkedin_account_id
    state.account_id = account_id
    bind_context(linkedin_account_id=account_id)
    if factory is None:
        budget = max(0, settings.scrape_max_pageloads_per_day - page_loads_today(db, account_id=account_id))
        factory = lambda s: _default_searcher_factory(s, account_id, budget)  # noqa: E731

    info["purged"] = seen_posts.purge_expired(db)
    db.commit()
    log.info("seen_posts_purged", rows=info["purged"])
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
    cap_kind = "post"  # which daily cap ended the run early: post | page | visit
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
                if state.fatal_llm is not None:
                    break
                if posts_seen_today(db, account_id=account_id) >= settings.scrape_max_posts_per_day and index < len(rows):
                    cap_hit = True
                    break
                if index < len(rows) and state.page_cap_reached():
                    cap_hit, cap_kind = True, "page"
                    break
            if settings.watch_append_to_discovery and state.fatal_llm is None and not cap_hit:
                # Prospect watch appended to this run (same browser session, same caps and cool-down).
                from sales0.jobs import watch_job

                watch_state = watch_job._WatchRun(db, run_id, settings, llm, sleep, rng, account_id)
                watch_rows = watch_job.plan_rows(db, run_id)
                rows.extend(watch_rows)
                run.keywords_planned += len(watch_rows)
                db.commit()
                if watch_rows:
                    cap_hit = watch_job.watch_phase(watch_state, searcher, watch_rows, sleep, rng, pause_first=True)
                    cap_kind = watch_state.cap_kind
                    state.fatal_llm = watch_state.fatal_llm
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

    finalise_run(
        db, state, rows, account_id, settings,
        abort_kind=abort_kind, abort_url=abort_url, session_missing=session_missing, open_error=open_error,
        cap_hit=cap_hit, cap_kind=cap_kind,
    )


def finalise_run(
    db: Session,
    state: _Run,
    rows: list[ScrapeRunKeyword],
    account_id: int | None,
    settings: Settings,
    *,
    abort_kind: str | None = None,
    abort_url: str | None = None,
    session_missing: bool = False,
    open_error: str | None = None,
    cap_hit: bool = False,
    cap_kind: str = "post",
) -> None:
    """Close a run: status, message, skipped keywords, account state. Shared with the hosted worker API
    (``services.worker_service.complete``), which reports the same outcomes the local job observes itself."""
    run = state.run
    now = utcnow()
    if abort_kind is not None:
        message = session_service.mark_needs_attention(db, abort_kind, abort_url, account_id=account_id)
        for row in rows:
            row_db = db.get(ScrapeRunKeyword, row.id)
            if row_db is not None and row_db.status == "error" and row_db.error_message is None:
                row_db.error_message = clip(f"Stopped: {SAFETY_LABELS.get(abort_kind, abort_kind)}", 300)
                row_db.errors += 1
                run.errors_count += 1
        state.skip_remaining(rows, f"Not searched — run stopped ({abort_kind})")
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
        run.current_step = clip(f"Run stopped: {SAFETY_LABELS.get(abort_kind, abort_kind)} — {summary_text(run)}", 200)
    elif session_missing:
        message = session_service.not_logged_in_message()
        session_service.mark_not_connected(db, message, account_id=account_id)
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
            posts_today = posts_seen_today(db, account_id=account_id)
            # abort_reason stays 'daily_post_cap' (the CHECK list is closed); the text says which cap.
            if cap_kind == "page":
                loads = page_loads_today(db, account_id=account_id)
                note = f"Daily page-load limit reached ({loads}/{settings.scrape_max_pageloads_per_day})"
                state.skip_remaining(rows, "Not searched — daily page-load limit reached")
            elif cap_kind == "visit":
                note = "Daily profile-visit limit reached"
                state.skip_remaining(rows, "Not searched — daily profile-visit limit reached")
            else:
                note = f"Daily post limit reached ({posts_today}/{settings.scrape_max_posts_per_day})"
                state.skip_remaining(rows, "Not searched — daily post limit reached")
            run = state.run
            run.abort_reason = "daily_post_cap"
            run.current_step = clip(f"{note} — {summary_text(run)}", 200)
        else:
            run.current_step = clip(f"Run finished — {summary_text(run)}", 200)
        state.set_status("succeeded")
        session_service.mark_successful_run(db, now=now, account_id=account_id)
    run.current_keyword = None
    run.finished_at = now
    db.flush()
    runs_service.sync_account_state(db, account_id, now)  # per-account counters + cool-down snapshot
    db.commit()




__all__ = ["execute", "relevance_pct", "select_keywords", "summary_text"]
