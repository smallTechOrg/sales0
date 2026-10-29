"""The LinkedIn company page of a prospect, read by the laptop worker (``worker_tasks`` kind ``company_page``).

Flow: the prospect page's Research button queues ONE task (``queue_for_prospect``) when the company has a LinkedIn
page and a worker is configured. A worker that advertises the ``company_page`` capability claims it on its next poll
(``claim_next``), visits that ONE page with the logged-in Chrome profile (same pacing and checkpoint/captcha
detection as a search) and uploads the visible text (``record_result``). The team then re-runs research / the
summary to include it. Nothing here runs automatically.

Account safety: a task is only handed out when the account could run a search right now - no other run in progress,
not disabled, no safety cool-down, the daily page-load cap not reached - plus its own daily cap
(``SALES0_COMPANY_PAGE_MAX_PER_DAY``). Its page loads count in ``runs.page_loads_today`` (the same daily counter as
discovery and the watch). A safety stop it reports (checkpoint / captcha / rate limit / logout) is recorded on the
account exactly like a Re-check stop, so the usual cool-down applies to every later run and task.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.clock import local_day_bounds_utc
from sales0.config import get_settings
from sales0.db.models import ScrapeRun, WorkerTask
from sales0.linkedin_url import canonical_linkedin
from sales0.logging import get_logger
from sales0.services import linkedin_accounts
from sales0.services._common import clip, now_or
from sales0.services.crm.actor import Actor
from sales0.worker import protocol as p

log = get_logger("sales0.research.linkedin_page")

CAPABILITY = "company_page"
LIVE_BASE = "https://www.linkedin.com"
MAX_TEXT_CHARS = 20_000
SAFETY_OUTCOMES = {"safety_stop"}


@dataclass(frozen=True)
class PageStatus:
    state: str  # none | unavailable | queued | claimed | fetched | failed
    detail: str
    task_id: int | None = None
    fetched_at: datetime | None = None
    requested_at: datetime | None = None
    url: str | None = None


def company_slug(url: str | None) -> str | None:
    """The ``<slug>`` of a ``linkedin.com/company/<slug>`` URL (fixture URLs only in fixture mode)."""
    found = canonical_linkedin(url, allow_fixture=get_settings().linkedin_mode == "fixture")
    if found is None or found[0] != "company":
        return None
    return found[1]


def _account_id(db: Session) -> int:
    return linkedin_accounts.default_account_id(db)


def tasks_today(db: Session, account_id: int, now: datetime | None = None) -> tuple[int, int]:
    """(company-page tasks handed out today, their page loads) for ONE account, local day."""
    start, end = local_day_bounds_utc(get_settings().tz, now_or(now))
    row = db.execute(
        select(func.count(WorkerTask.id), func.coalesce(func.sum(WorkerTask.page_loads), 0)).where(
            WorkerTask.claimed_at >= start, WorkerTask.claimed_at < end, WorkerTask.linkedin_account_id == account_id
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


def _latest(db: Session, prospect_id: int, *, status: tuple[str, ...] | None = None) -> WorkerTask | None:
    q = select(WorkerTask).where(WorkerTask.prospect_id == prospect_id, WorkerTask.kind == "company_page")
    if status:
        q = q.where(WorkerTask.status.in_(status))
    return db.scalar(q.order_by(WorkerTask.id.desc()).limit(1))


def latest_text(db: Session, prospect_id: int) -> WorkerTask | None:
    """The newest fetched company page of this prospect (or of its company), if any."""
    row = _latest(db, prospect_id, status=("done",))
    if row is not None and row.result_text:
        return row
    company_id = db.scalar(select(WorkerTask.company_id).where(WorkerTask.prospect_id == prospect_id).limit(1))
    if company_id is None:
        from sales0.db.models import Prospect

        prospect = db.get(Prospect, prospect_id)
        company_id = prospect.company_id if prospect else None
    if company_id is None:
        return None
    return db.scalar(
        select(WorkerTask)
        .where(WorkerTask.company_id == company_id, WorkerTask.status == "done", WorkerTask.result_text.is_not(None))
        .order_by(WorkerTask.id.desc())
        .limit(1)
    )


def _expire_stale(db: Session, now: datetime) -> None:
    cutoff = now - timedelta(seconds=get_settings().company_page_stale_s)
    for row in db.scalars(select(WorkerTask).where(WorkerTask.status == "claimed", WorkerTask.claimed_at < cutoff)).all():
        row.status, row.outcome, row.finished_at = "failed", "lost", now
        row.error = "The laptop worker did not report back - press Research again to retry."


def status_for(db: Session, prospect_id: int, company_url: str | None, now: datetime | None = None) -> PageStatus:
    now = now_or(now)
    _expire_stale(db, now)
    fetched = latest_text(db, prospect_id)
    last = _latest(db, prospect_id)
    if last is not None and last.status in ("queued", "claimed"):
        detail = "pending - the laptop worker picks it up on its next poll" if last.status == "queued" else "being read by the laptop worker"
        if fetched is not None:
            detail += f" (last fetched {fetched.finished_at:%d %b %H:%M} UTC)" if fetched.finished_at else ""
        return PageStatus(last.status, detail, last.id, fetched.finished_at if fetched else None, last.created_at, last.target_url)
    if fetched is not None:
        return PageStatus("fetched", "fetched", fetched.id, fetched.finished_at, fetched.created_at, fetched.result_url or fetched.target_url)
    if last is not None and last.status == "failed":
        return PageStatus("failed", last.error or "The page could not be read.", last.id, None, last.created_at, last.target_url)
    if not company_slug(company_url):
        return PageStatus("none", "No LinkedIn company page on the company - add it under Edit details.")
    if not get_settings().worker_token_value:
        return PageStatus("unavailable", "Needs the laptop worker (SALES0_WORKER_TOKEN) - not configured here.")
    return PageStatus("none", "Not fetched yet - press Research to queue it for the laptop worker.")


def queue_for_prospect(
    db: Session, prospect_id: int, company_id: int | None, company_url: str | None, *, actor: Actor, now: datetime | None = None
) -> tuple[WorkerTask | None, str]:
    """Queue (at most) one company-page task. Returns (task or None, a sentence for the UI)."""
    now = now_or(now)
    settings = get_settings()
    slug = company_slug(company_url)
    if slug is None:
        return None, "No LinkedIn company page is set for this company, so only the website, our records and the web were used."
    if not settings.worker_token_value:
        return None, "The LinkedIn company page needs the laptop worker, which is not configured here."
    if settings.company_page_max_per_day <= 0:
        return None, "LinkedIn company-page visits are turned off (SALES0_COMPANY_PAGE_MAX_PER_DAY=0)."
    _expire_stale(db, now)
    pending = _latest(db, prospect_id, status=("queued", "claimed"))
    if pending is not None:
        return pending, "The LinkedIn company page is already queued for the laptop worker."
    task = WorkerTask(
        kind="company_page", status="queued", prospect_id=prospect_id, company_id=company_id,
        target_url=f"{LIVE_BASE}/company/{slug}/", requested_by_member_id=actor.member_id, created_at=now,
    )  # fmt: skip
    db.add(task)
    db.flush()
    log.info("company_page_queued", task_id=task.id, prospect_id=prospect_id)
    return task, "The LinkedIn company page was queued for the laptop worker; press Research again once it shows as fetched."


# ---------------------------------------------------------------------- worker side
def _refuse(db: Session, now: datetime, account_id: int) -> str | None:
    """Why no company-page task may be handed out right now (None = it may)."""
    from sales0.services import runs as runs_service

    settings = get_settings()
    account = linkedin_accounts.get_account(db, account_id)
    if account.disabled:
        return f"LinkedIn account '{account.label}' is disabled"
    until = runs_service.safety_cooldown_until(db, now, account_id)
    if until is not None:
        return runs_service.cooldown_reason(until, now)
    if runs_service.page_loads_today(db, now, account_id) >= settings.scrape_max_pageloads_per_day:
        return "Daily LinkedIn page-load limit reached - try again tomorrow"
    if tasks_today(db, account_id, now)[0] >= settings.company_page_max_per_day:
        return f"Daily LinkedIn company-page limit reached ({settings.company_page_max_per_day}) - try again tomorrow"
    return None


def claim_next(db: Session, worker_name: str, now: datetime | None = None) -> p.ClaimedTask | None:
    """Hand the oldest queued task to a capable worker, or None. A task that cannot run (cool-down, cap,
    disabled account) is FAILED with the reason so the prospect page says why; it waits while a run is in
    progress. Commits."""
    now = now_or(now)
    _expire_stale(db, now)
    task = db.scalar(select(WorkerTask).where(WorkerTask.status == "queued").order_by(WorkerTask.id).limit(1))
    if task is None:
        db.commit()
        return None
    if db.scalar(select(ScrapeRun.id).where(ScrapeRun.status == "running").limit(1)) is not None:
        db.commit()
        return None  # one browser job at a time: after the run
    account_id = _account_id(db)
    reason = _refuse(db, now, account_id)
    if reason:
        task.status, task.outcome, task.finished_at, task.error = "failed", "refused", now, clip(reason, 500)
        task.linkedin_account_id = account_id
        db.commit()
        log.info("company_page_refused", task_id=task.id, reason=reason)
        return None
    slug = company_slug(task.target_url)
    if slug is None:  # cannot happen for rows we wrote; never hand an arbitrary URL to the browser
        task.status, task.outcome, task.finished_at, task.error = "failed", "invalid", now, "Not a LinkedIn company page URL"
        db.commit()
        return None
    task.status, task.claimed_at, task.worker_name, task.linkedin_account_id = "claimed", now, worker_name[:80], account_id
    task.page_loads = 1  # counted when handed out: the visit is attempted (a failed load still hit LinkedIn)
    db.commit()
    log.info("company_page_claimed", task_id=task.id, worker=worker_name)
    return p.ClaimedTask(task_id=task.id, kind="company_page", slug=f"company/{slug}")


def record_result(db: Session, task_id: int, req: p.TaskResultRequest, *, now: datetime | None = None) -> p.TaskResultResponse:
    """The worker's report. Idempotent (a retry after a lost answer returns the stored status). Commits."""
    from sales0.errors import DomainError

    now = now_or(now)
    task = db.get(WorkerTask, task_id)
    if task is None:
        raise DomainError("not_found", "That task no longer exists.", 404)
    if task.status not in ("claimed", "queued"):
        return p.TaskResultResponse(status=task.status)
    account_id = task.linkedin_account_id or _account_id(db)
    task.finished_at, task.outcome = now, req.outcome
    if req.page_loads is not None:
        task.page_loads = max(task.page_loads, min(req.page_loads, 5))
    if req.outcome == "done" and (req.text or "").strip():
        task.status = "done"
        task.result_text = req.text[:MAX_TEXT_CHARS]
        task.result_title = clip(req.title, 300)
        task.result_url = clip(req.url, 1000) if (req.url or "").startswith(("https://", "http://")) else task.target_url
        task.error = None
        linkedin_accounts.mark_connected(db, account_id, now=now)
    else:
        task.status = "failed"
        if req.outcome == "done":
            task.outcome = "empty"
        task.error = clip(_message(req), 500)
        if req.outcome in SAFETY_OUTCOMES:
            kind = req.kind if req.kind in ("logout", "checkpoint", "captcha", "rate_limited") else "rate_limited"
            linkedin_accounts.mark_needs_attention(
                db, kind, f"LinkedIn {kind.replace('_', ' ')} while reading a company page - paused for safety", account_id, now=now
            )
        elif req.outcome == "session_missing":
            linkedin_accounts.mark_not_connected(db, "Not logged in to LinkedIn on the laptop", account_id, now=now)
    db.commit()
    log.info("company_page_result", task_id=task.id, status=task.status, outcome=req.outcome, chars=len(task.result_text or ""))
    return p.TaskResultResponse(status=task.status)


def _message(req: p.TaskResultRequest) -> str:
    return {
        "done": "The page had no readable text.",
        "safety_stop": f"LinkedIn showed a {(req.kind or 'safety').replace('_', ' ')} page - the account is paused (cool-down).",
        "session_missing": "The laptop is not logged in to LinkedIn.",
        "open_failed": "The laptop worker could not open LinkedIn.",
        "page_error": "LinkedIn did not show the company page.",
        "crashed": "The laptop worker failed unexpectedly.",
    }.get(req.outcome, "The page could not be read.") + (f" ({req.error})" if req.error else "")


__all__ = [
    "CAPABILITY", "PageStatus", "claim_next", "company_slug", "latest_text", "queue_for_prospect", "record_result",
    "status_for", "tasks_today",
]
