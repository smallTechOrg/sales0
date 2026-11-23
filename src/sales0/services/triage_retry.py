"""Retry all failed AI triage in one click: the Inbox's "To review" enquiries the AI could not read (or never read,
e.g. the spend cap stopped a run) and the "Skipped by AI" posts whose triage failed.

One request, at most ``MAX_PER_CLICK`` items, one AI call each, every outcome committed as it happens (the same
calls as each card's Retry triage button). It stops early when retrying is pointless: the AI key is rejected, the
spend cap is reached, or ``STOP_AFTER_FAILURES`` in a row fail (the AI looks down). Items left over are picked up by
the next click.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.db.models import Enquiry, SeenPost
from sales0.errors import DomainError
from sales0.jobs.runner import get_runner, job_busy_error
from sales0.logging import get_logger
from sales0.services.costs_report import SpendCapReached

log = get_logger("sales0.triage_retry")

MAX_PER_CLICK = 25
STOP_AFTER_FAILURES = 3
_FATAL_CODES = frozenset({"llm_auth", "spend_cap_reached"})


@dataclass(frozen=True)
class RetryAllResult:
    done: int
    failed: int
    left: int  # still to retry after this click
    stopped: str | None = None  # why it stopped early, if it did
    last_error: str | None = None

    @property
    def message(self) -> str:
        if self.done == 0 and self.failed == 0 and self.stopped is None:
            return "Nothing to retry."
        parts = [f"AI triage retried: {self.done} done"]
        if self.failed:
            parts.append(f"{self.failed} failed again")
        text = ", ".join(parts) + "."
        if self.stopped:
            text += f" Stopped: {self.stopped}"
        elif self.failed and self.last_error:
            text += f" Last error: {self.last_error}"
        if self.left:
            text += f" {self.left} still to retry - press Retry all again{' once the AI works' if self.stopped else ''}."
        return text

    @property
    def ok(self) -> bool:
        return self.stopped is None and self.failed == 0


def _enquiry_filter():  # type: ignore[no-untyped-def]
    return (Enquiry.triage_status.in_(("failed", "pending")), Enquiry.review_status != "discarded")


def _post_filter():  # type: ignore[no-untyped-def]
    return (SeenPost.triage_status == "failed", SeenPost.enquiry_id.is_(None), SeenPost.raw_text.is_not(None))


def enquiries_to_retry(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(Enquiry).where(*_enquiry_filter())) or 0)


def posts_to_retry(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(SeenPost).where(*_post_filter())) or 0)


def _run(db: Session, ids: list[int], total: int, retry_one: Callable[[int], object]) -> RetryAllResult:
    if get_runner().busy() == "run":
        raise job_busy_error("run")  # a run is triaging its own enquiries right now
    done = failed = streak = 0
    stopped: str | None = None
    last_error: str | None = None
    for item_id in ids:
        try:
            retry_one(item_id)
        except SpendCapReached as exc:
            stopped = exc.message
            break
        except DomainError as exc:
            if exc.code == "not_found":
                continue  # deleted meanwhile
            if exc.code == "decision_invalid":
                done += 1  # triaged meanwhile (e.g. by another click)
                continue
            db.rollback()
            if exc.code in _FATAL_CODES or exc.code == "job_busy":
                stopped = exc.message
                break
            failed += 1
            streak += 1
            last_error = exc.message
            if streak >= STOP_AFTER_FAILURES:
                stopped = f"{STOP_AFTER_FAILURES} retries in a row failed, so the AI looks unavailable ({exc.message})"
                break
            continue
        done += 1
        streak = 0
    left = max(0, total - done)
    log.info("triage_retry_all", done=done, failed=failed, left=left, stopped=bool(stopped))
    return RetryAllResult(done=done, failed=failed, left=left, stopped=stopped, last_error=last_error)


def retry_failed_enquiries(db: Session) -> RetryAllResult:
    from sales0.services import inbox as inbox_service

    total = enquiries_to_retry(db)
    ids = list(db.scalars(select(Enquiry.id).where(*_enquiry_filter()).order_by(Enquiry.id).limit(MAX_PER_CLICK)))
    return _run(db, ids, total, lambda i: inbox_service.retry_triage(db, i))


def retry_failed_posts(db: Session) -> RetryAllResult:
    from sales0.services import seen_posts as seen_posts_service

    total = posts_to_retry(db)
    ids = list(db.scalars(select(SeenPost.id).where(*_post_filter()).order_by(SeenPost.id).limit(MAX_PER_CLICK)))

    def one(post_id: int) -> object:
        view = seen_posts_service.retry_triage(db, post_id)
        if getattr(view, "triage_status", "done") == "failed":
            raise DomainError("triage_failed", getattr(view, "triage_error", None) or "AI triage failed.", 502)
        return view

    return _run(db, ids, total, one)


__all__ = ["MAX_PER_CLICK", "RetryAllResult", "enquiries_to_retry", "posts_to_retry", "retry_failed_enquiries", "retry_failed_posts"]
