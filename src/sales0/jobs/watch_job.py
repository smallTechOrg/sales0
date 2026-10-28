"""Prospect watch run: visit due tracked profiles and push their NEW posts through the same
dedupe -> triage-v3 -> store-only-if-relevant pipeline as discovery.

A watch run is a ``scrape_runs`` row (marked by ``watch_runs``) executed on the JobRunner's single
browser slot, so it can never overlap a discovery run, a Connect or a Re-check. Per-profile log
rows are ``scrape_run_keywords`` rows (``watch:<slug>``): checked, posts seen, new, relevant, errors.

Reuses ``discovery_job._Run`` for registering sightings, concurrent triage (with the spend-cap
guard) and persistence; only the page reader (``scan_profile``) and the profile bookkeeping differ.
New relevant posts become enquiries linked to the profile (``enquiries.author_profile_id``; the
keyword hit text ``watch:<slug>`` is the source tag) and a ``TrackedProfileHit`` is published.
Safety is shared with discovery: checkpoint/captcha/logout/429 stop the run (session
``needs_attention`` -> the global cool-down applies), pacing, and the per-day scanned-post cap.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from functools import partial
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from sales0.clock import utcnow
from sales0.config import Settings, get_settings
from sales0.db.engine import get_sessionmaker
from sales0.db.models import LinkedinProfile, ScrapeRun, ScrapeRunKeyword, SeenPost, WatchRun
from sales0.jobs import discovery_job
from sales0.jobs.discovery_job import SAFETY_LABELS, _Run
from sales0.jobs.session_log import log_run_start
from sales0.llm import LLMClient
from sales0.logging import bind_context, get_logger, unbind_context
from sales0.scraper.errors import AccountSafetyStop, ScrapeError, SessionMissing
from sales0.scraper.profile_watch import own_posts, scan_profile
from sales0.services import events, linkedin_accounts, seen_posts
from sales0.services import runs as runs_service
from sales0.services import session as session_service
from sales0.services import tracked_profiles as tracked
from sales0.services._common import clip
from sales0.services.runs import posts_seen_today

log = get_logger("sales0.jobs.watch")

SearcherFactory = Callable[[Settings], Any]


def plan_rows(db: Session, run_id: int) -> list[ScrapeRunKeyword]:
    """Add one log row per due profile (least-recently-checked first, capped) to run ``run_id``."""
    rows = []
    for profile in tracked.due_profiles(db):
        rows.append(
            ScrapeRunKeyword(
                run_id=run_id, keyword_id=None, keyword_text=clip(tracked.WATCH_PREFIX + profile.slug, 100) or "", status="pending"
            )
        )
    db.add_all(rows)
    return rows


class _WatchRun(_Run):
    """``_Run`` plus the per-profile step."""

    hits = 0

    def scrape_profile(self, searcher: Any, row: ScrapeRunKeyword, index: int, total: int) -> None:
        slug = row.keyword_text.removeprefix(tracked.WATCH_PREFIX)
        profile = self.db.scalar(select(LinkedinProfile).where(LinkedinProfile.slug == slug))
        if profile is None or profile.tracked is not True:  # untracked since planning: never visit
            row.status = "skipped"
            row.error_message = "No longer tracked"
            self.db.commit()
            return
        profile_id, profile_url = profile.id, profile.profile_url
        started = time.monotonic()
        row.status = "running"
        self.step(f"Checking {profile.display_name or slug} ({index}/{total})", slug)
        remaining = self.settings.scrape_max_posts_per_day - posts_seen_today(self.db, account_id=self.account_id)
        max_posts = max(1, min(self.settings.watch_max_posts_per_profile, remaining))
        scan = getattr(searcher, "scan_profile", None) or partial(scan_profile, searcher)
        try:
            result = scan(slug, max_posts)
        except AccountSafetyStop:
            row.status = "error"
            row.duration_ms = int((time.monotonic() - started) * 1000)
            tracked.record_check(self.db, profile_id, "stopped")
            self.db.commit()
            raise
        except (ScrapeError, ValueError) as exc:
            row.status = "error"
            row.errors += 1
            row.error_message = clip(f"Page error: {exc}", 300)
            row.duration_ms = int((time.monotonic() - started) * 1000)
            self.run.errors_count += 1
            self.run.keywords_done += 1
            tracked.record_check(self.db, profile_id, "error")
            self.db.commit()
            return

        posts, foreign = own_posts(list(result.posts), slug, profile_url)
        keyword_id: int | None = None
        to_triage: list[int] = []
        for post in posts:
            try:
                sighting, _ = self._register(post, row, keyword_id)
            except Exception as exc:  # noqa: BLE001 - a malformed post must not lose the others
                log.exception("watch_post_save_failed", error_type=type(exc).__name__)
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
            if (sighting.kind == "new" or sighting.triage_status == "pending") and sighting.seen_post_id not in to_triage:
                to_triage.append(sighting.seen_post_id)
        self.db.commit()

        self.triage_batch(row, to_triage, keyword_id, set())
        hits = self._publish_hits(profile_id, to_triage)

        notes = []
        status, check = "done", "ok"
        if result.drift_warning:
            status, check = "warning", "drift"
            notes.append(f"No posts parsed - LinkedIn layout may have changed ({result.cards_found} cards found)")
        elif not posts and not foreign:
            check = "no_posts"
            notes.append("No posts on the page")
        if foreign:
            notes.append(f"{foreign} post{'s' if foreign != 1 else ''} by others skipped")
        if result.parse_errors:
            row.errors += int(result.parse_errors)
            self.run.errors_count += int(result.parse_errors)
        if notes:
            row.error_message = clip(" · ".join(notes), 300)
        row.status = status
        row.duration_ms = int((time.monotonic() - started) * 1000)
        self.run.keywords_done += 1
        tracked.record_check(self.db, profile_id, check, hits=hits)
        self.db.commit()
        log.info(
            "watch_profile", slug_kind=slug.split("/")[0], posts_seen=row.posts_seen, posts_new=row.posts_new,
            posts_relevant=row.posts_relevant, errors=row.errors, hits=hits, cards_found=result.cards_found,
            strategy=str(result.strategy), duration_ms=row.duration_ms, status=check,
        )

    def _publish_hits(self, profile_id: int, seen_post_ids: list[int]) -> int:
        """One ``TrackedProfileHit`` per post of this visit that became an enquiry."""
        if not seen_post_ids:
            return 0
        enquiry_ids = [
            int(e)
            for e in self.db.scalars(
                select(SeenPost.enquiry_id).where(
                    SeenPost.id.in_(seen_post_ids), SeenPost.verdict == "genuine", SeenPost.enquiry_id.is_not(None)
                )
            )
        ]
        for eid in enquiry_ids:
            events.publish(self.db, events.TrackedProfileHit(profile_id=profile_id, enquiry_id=eid, run_id=self.run_id))
        if enquiry_ids:
            self.db.execute(update(WatchRun).where(WatchRun.run_id == self.run_id).values(hits=WatchRun.hits + len(enquiry_ids)))
            self.hits += len(enquiry_ids)
        self.db.commit()
        return len(enquiry_ids)


def watch_phase(
    state: _WatchRun, searcher: Any, rows: list[ScrapeRunKeyword], sleep: Callable[[float], None], rng: random.Random,
    *, pause_first: bool = False,
) -> bool:
    """Visit ``rows`` in order with human pacing. Returns True when the daily post cap ended it early.
    ``AccountSafetyStop`` propagates (the caller owns the run outcome)."""
    s = state.settings
    for index, row in enumerate(rows, start=1):
        if index > 1 or pause_first:
            seconds = rng.uniform(s.watch_profile_pause_min_s, s.watch_profile_pause_max_s)
            state.step(f"Pausing {round(seconds)}s before next profile")
            sleep(seconds)
        state.scrape_profile(searcher, row, index, len(rows))
        if state.fatal_llm is not None:
            return False
        if posts_seen_today(state.db, account_id=state.account_id) >= s.scrape_max_posts_per_day and index < len(rows):
            return True
    return False


def summary_text(run: ScrapeRun, hits: int) -> str:
    return (
        f"checked {run.keywords_done} profile{'s' if run.keywords_done != 1 else ''} · scanned {run.posts_seen} · "
        f"new {run.posts_new} · relevant {run.posts_relevant} · hits {hits}"
    )


def execute(
    run_id: int,
    *,
    llm: LLMClient | None = None,
    searcher_factory: SearcherFactory | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> None:
    """Run a queued watch run. Never raises: every outcome is written to the run row."""
    settings = get_settings()
    factory = searcher_factory
    bind_context(run_id=run_id, job="watch")
    started = time.monotonic()
    db = get_sessionmaker()()
    try:
        _execute(db, run_id, settings, llm, factory, sleep, rng or random.Random())
    except Exception as exc:  # noqa: BLE001 - the run must never stay "running"
        log.exception("watch_run_crashed", error_type=type(exc).__name__)
        db.rollback()
        discovery_job._finalise_crash(db, run_id, f"Watch run failed unexpectedly ({type(exc).__name__}) - see the server log")
    finally:
        run = db.get(ScrapeRun, run_id)
        if run is not None:
            log.info(
                "watch_run", status=run.status, profiles_planned=run.keywords_planned, profiles_done=run.keywords_done,
                posts_seen=run.posts_seen, posts_new=run.posts_new, posts_relevant=run.posts_relevant,
                errors_count=run.errors_count, abort_reason=run.abort_reason,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        db.close()
        unbind_context("run_id", "linkedin_account_id")


def _default_factory(settings: Settings, account_id: int | None = None) -> Any:
    """A ``LinkedInSearcher`` on the run's LinkedIn account (None = the default account)."""
    from sales0.scraper.search import LinkedInSearcher

    return LinkedInSearcher(settings, account=linkedin_accounts.build_handle(account_id))


def _execute(
    db: Session, run_id: int, settings: Settings, llm: LLMClient | None, factory: SearcherFactory | None,
    sleep: Callable[[float], None], rng: random.Random,
) -> None:
    state = _WatchRun(db, run_id, settings, llm, sleep, rng)
    run = db.get(ScrapeRun, run_id)
    if run is None or run.status != "queued":
        log.warning("watch_run_not_queued", status=getattr(run, "status", None))
        return
    if run.linkedin_account_id is None:  # queued by older code / a test: stamp the default account
        run.linkedin_account_id = linkedin_accounts.default_account_id(db)
        db.flush()
    account_id = run.linkedin_account_id
    state.account_id = account_id
    bind_context(linkedin_account_id=account_id)
    if factory is None:
        factory = lambda s: _default_factory(s, account_id)  # noqa: E731
    seen_posts.purge_expired(db)
    rows = plan_rows(db, run_id)
    run.keywords_planned = len(rows)
    state.set_status("running")
    run.current_step = "Opening LinkedIn and checking the session"
    db.commit()
    if not rows:
        run.current_step = "No tracked profile is due for a check"
        state.set_status("succeeded")
        run.finished_at = utcnow()
        db.commit()
        return

    abort_kind: str | None = None
    abort_url: str | None = None
    open_error: str | None = None
    session_missing = False
    cap_hit = False
    entered = False
    try:
        with factory(settings) as searcher:
            entered = True
            check = getattr(searcher, "session", None)
            log_run_start("connected", "ok", url=getattr(check, "url", None), cookie_present=True,
                          soft_hints=tuple(getattr(check, "soft_hints", ()) or ()))
            cap_hit = watch_phase(state, searcher, rows, sleep, rng)
    except SessionMissing as missing:
        db.rollback()
        session_missing = True
        log_run_start("not_connected", "no_cookie", url=getattr(missing, "url", None), cookie_present=False)
    except AccountSafetyStop as stop:
        db.rollback()
        abort_kind = stop.kind
        abort_url = stop.url
        if not entered:
            log_run_start("needs_attention", stop.kind, url=stop.url, cookie_present=True)
        log.warning("watch_safety_stop", kind=stop.kind, url=abort_url, at_run_start=not entered)
    except ScrapeError as exc:
        db.rollback()
        open_error = clip(f"Could not open LinkedIn: {exc}", 500)
        log.error("watch_open_failed", error=str(exc)[:200])

    run = state.run
    now = utcnow()
    if abort_kind is not None:
        message = session_service.mark_needs_attention(db, abort_kind, abort_url, account_id=account_id)
        for r in rows:
            row_db = db.get(ScrapeRunKeyword, r.id)
            if row_db is not None and row_db.status == "error" and row_db.error_message is None:
                row_db.error_message = clip(f"Stopped: {SAFETY_LABELS.get(abort_kind, abort_kind)}", 300)
                row_db.errors += 1
                run.errors_count += 1
        state.skip_remaining(rows, f"Not checked - run stopped ({abort_kind})")
        run = state.run
        run.abort_reason = abort_kind
        label = SAFETY_LABELS.get(abort_kind, abort_kind)
        run.error_message = clip(f"Watch run stopped: {label}. {message}", 500)
        state.set_status("aborted", note=abort_kind)
        run.current_step = clip(f"Run stopped: {label} - {summary_text(run, state.hits)}", 200)
    elif session_missing:
        message = session_service.NOT_LOGGED_IN_MESSAGE
        session_service.mark_not_connected(db, message, account_id=account_id)
        state.skip_remaining(rows, "Not checked - not logged in")
        run = state.run
        run.error_message = message
        state.set_status("failed", note="not_logged_in")
        run.current_step = "Run failed - not logged in to LinkedIn in sales0's browser"
    elif open_error is not None:
        state.skip_remaining(rows, "Not checked - LinkedIn could not be opened")
        run = state.run
        run.error_message = open_error
        run.errors_count += 1
        state.set_status("failed", note="open_failed")
        run.current_step = "Run failed - LinkedIn could not be opened"
    elif state.fatal_llm is not None:
        state.skip_remaining(rows, "Not checked - AI triage stopped")
        run = state.run
        run.error_message = clip(state.fatal_llm, 500)
        state.set_status("failed", note="llm_fatal")
        run.current_step = "AI triage stopped"
    else:
        if cap_hit:
            state.skip_remaining(rows, "Not checked - daily post limit reached")
            run = state.run
            run.abort_reason = "daily_post_cap"
            run.current_step = clip(
                f"Daily post limit reached ({posts_seen_today(db, account_id=account_id)}/{settings.scrape_max_posts_per_day}) - {summary_text(run, state.hits)}", 200
            )
        else:
            run.current_step = clip(f"Watch finished - {summary_text(run, state.hits)}", 200)
        state.set_status("succeeded")
        session_service.mark_successful_run(db, now=now, account_id=account_id)
    run.current_keyword = None
    run.finished_at = now
    db.flush()
    runs_service.sync_account_state(db, account_id, now)  # per-account counters + cool-down snapshot
    db.commit()


__all__ = ["execute", "plan_rows", "summary_text", "watch_phase"]
