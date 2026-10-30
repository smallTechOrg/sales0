"""Automatic and bulk research: the company, the enquiry and the people behind it, for every ACTIVE enquiry.

* **On accept** (``kick_for_enquiries``): the Inbox route calls it after the commit. It starts one background thread
  that researches each affected prospect (its own session per prospect, one version each). Nothing here can fail the
  accept: errors are logged and the prospect simply stays "pending".
* **Bulk** (``run_pending``): the Pipeline button. Researches every prospect that is pending, one at a time, committing
  after each, and stops at the item limit / time budget (hosted: ``SALES0_HOSTED_ACTION_BUDGET_S``) with "N remaining".
* **Pending** is derived from the data, there is no queue table: an in-scope prospect needs research when it has no
  usable research, its newest research predates the enquiry/people sections (``schema`` < 3), or an active enquiry or
  contact is not among the ones the newest research covered. A newest version that a person edited is never replaced
  automatically (press "Re-run research" on the company to do that on purpose).
* **In scope** = prospects with at least one accepted enquiry that is not won / lost / no response / on hold, whose
  prospect is not closed and whose company is not off-ramped.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.agent.research import SafeFetcher
from sales0.config import get_settings
from sales0.db.engine import session_scope
from sales0.db.models import Company, Enquiry, Prospect
from sales0.errors import DomainError
from sales0.llm import LLMAuthError, LLMClient, LLMError, LLMModelNotFound, get_llm_client
from sales0.logging import get_logger
from sales0.services._common import now_or
from sales0.services.costs_report import SpendCapReached
from sales0.services.crm.actor import SYSTEM, Actor
from sales0.services.research import context, docs
from sales0.services.research import prospect as research_service

log = get_logger("sales0.research.auto")

REASON_LABELS = {
    "none": "no research yet",
    "old_format": "research predates the enquiry / people sections",
    "new_enquiry": "a new enquiry since the last research",
    "new_contact": "a new contact since the last research",
}
MAX_CONSECUTIVE_FAILURES = 2


@dataclass
class Outcome:
    prospect_id: int
    name: str
    status: str  # researched | failed
    reason: str | None = None  # why it was pending, or the failure
    version: int | None = None


@dataclass
class BulkResult:
    outcomes: list[Outcome] = field(default_factory=list)
    remaining: int = 0
    stopped: str | None = None

    @property
    def researched(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "researched")

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "failed")


    def summary(self) -> str:
        if not self.outcomes and not self.stopped:
            return "Nothing to research - every active enquiry already has up-to-date research."
        n = self.researched
        parts = [f"Researched {n} compan{'y' if n == 1 else 'ies'} (company, enquiry and people)"]
        if self.failed:
            parts.append(f"{self.failed} failed")
        text = ", ".join(parts) + "."
        if self.remaining:
            text += f" {self.remaining} more pending - click again."
        if self.stopped:
            text += f" Stopped: {self.stopped}"
        return text[:290]


@dataclass
class Overview:
    in_scope: int = 0
    up_to_date: int = 0
    human_edited: int = 0  # newest version was edited by a person: never replaced automatically
    pending: dict[int, str] = field(default_factory=dict)  # prospect_id -> reason

    @property
    def pending_count(self) -> int:
        return len(self.pending)


# ---------------------------------------------------------------------- what is pending
def in_scope_prospect_ids(db: Session) -> list[int]:
    stmt = (
        select(Enquiry.prospect_id).distinct()
        .join(Prospect, Prospect.id == Enquiry.prospect_id)
        .outerjoin(Company, Company.id == Prospect.company_id)
        .where(
            Enquiry.review_status == "accepted", Enquiry.prospect_id.is_not(None), Prospect.status != "closed",
            Company.off_ramp.is_(None) | Company.id.is_(None),
            Enquiry.deal_status.is_(None) | Enquiry.deal_status.not_in(context.PARKED_DEAL_STATUSES),
        )
        .order_by(Enquiry.prospect_id)
    )  # fmt: skip
    return [int(i) for i in db.scalars(stmt)]


def reason_for(db: Session, prospect_id: int) -> str | None:
    """Why the prospect needs research now, or ``None`` (up to date, or newest version is a person's edit)."""
    usable = docs.latest_usable(db, prospect_id, "research")
    if usable is None:
        return "none"
    if usable.origin == "human":
        return None
    if int(usable.inputs.get("schema") or 0) < research_service.RESEARCH_SCHEMA:
        return "old_format"
    if not set(context.active_enquiry_ids(db, prospect_id)) <= set(usable.inputs.get("enquiry_ids") or []):
        return "new_enquiry"
    if not set(context.contact_ids(db, prospect_id)) <= set(usable.inputs.get("contact_ids") or []):
        return "new_contact"
    return None


def overview(db: Session) -> Overview:
    out = Overview()
    for pid in in_scope_prospect_ids(db):
        out.in_scope += 1
        reason = reason_for(db, pid)
        if reason is not None:
            out.pending[pid] = reason
            continue
        usable = docs.latest_usable(db, pid, "research")
        if usable is not None and usable.origin == "human":
            out.human_edited += 1
        else:
            out.up_to_date += 1
    return out


# ---------------------------------------------------------------------- one prospect
def default_fetcher() -> SafeFetcher | None:
    hosts = get_settings().research_private_hosts  # [] unless fixture mode + the explicit test switch
    return SafeFetcher(allow_private_hosts=hosts) if hosts else None


def _research_one(db: Session, llm: LLMClient | None, prospect_id: int, trigger: str, actor: Actor, now: datetime | None) -> int:
    fetcher = default_fetcher()
    try:
        doc, _ = research_service.research_prospect(db, llm, None, prospect_id, actor=actor, fetcher=fetcher, now=now, trigger=trigger)
    finally:
        if fetcher is not None:
            fetcher.close()
    return doc.version


# ---------------------------------------------------------------------- bulk (the Pipeline button)
def run_pending(
    db: Session,
    llm: LLMClient | None,
    *,
    limit: int | None = None,
    budget_s: float | None = None,
    prospect_ids: list[int] | None = None,
    actor: Actor = SYSTEM,
    trigger: str = "bulk",
    now: datetime | None = None,
) -> BulkResult:
    """Research the pending in-scope prospects (optionally only ``prospect_ids``), one at a time, committing after each.

    Stops at ``limit`` researched items or when ``budget_s`` has elapsed (hosted default: the action budget) and counts
    the rest as ``remaining``. ``LLMAuthError`` / ``LLMModelNotFound`` propagate (their error rows are committed);
    the spend cap or two failures in a row stop the run with a message."""
    settings = get_settings()
    if budget_s is None and settings.is_hosted:
        budget_s = float(settings.hosted_action_budget_s)
    started = time.monotonic()
    stamp = now_or(now)
    wanted = overview(db).pending
    if prospect_ids is not None:
        wanted = {pid: why for pid, why in wanted.items() if pid in set(prospect_ids)}
    result = BulkResult()
    failures = 0
    queue = list(wanted.items())
    for index, (pid, why) in enumerate(queue):
        if (limit is not None and result.researched >= limit) or (budget_s is not None and time.monotonic() - started > budget_s):
            result.remaining = len(queue) - index
            break
        p = db.get(Prospect, pid)
        name = p.name if p is not None else f"prospect {pid}"
        try:
            version = _research_one(db, llm, pid, trigger, actor, stamp)
            db.commit()
            result.outcomes.append(Outcome(pid, name, "researched", REASON_LABELS.get(why, why), version))
            failures = 0
        except SpendCapReached as exc:
            db.rollback()
            result.stopped = exc.message
            result.remaining = len(queue) - index
            break
        except (LLMAuthError, LLMModelNotFound):
            db.commit()  # keep the error llm_calls row
            raise
        except (DomainError, LLMError) as exc:
            db.rollback()
            result.outcomes.append(Outcome(pid, name, "failed", getattr(exc, "message", None) or "The AI service is unavailable."))
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                result.stopped = "the AI service keeps failing - try again in a minute."
                result.remaining = len(queue) - index - 1
                break
    log.info("research_bulk_done", researched=result.researched, failed=result.failed, remaining=result.remaining)
    return result


# ---------------------------------------------------------------------- on accept (background)
_lock = threading.Lock()
_inflight: set[int] = set()


def _llm() -> LLMClient:
    """Seam: tests replace this with a fake client."""
    return get_llm_client()


def _spawn(work: Callable[[], None]) -> None:
    """Seam: tests run the work inline instead of in a thread."""
    threading.Thread(target=work, name="sales0-auto-research", daemon=True).start()


def _work(prospect_ids: list[int]) -> None:
    for pid in prospect_ids:
        with _lock:
            if pid in _inflight:
                continue
            _inflight.add(pid)
        try:
            with session_scope() as db:
                why = reason_for(db, pid)
                if why is None:
                    continue
                version = _research_one(db, _llm(), pid, "accepted", SYSTEM, None)
                log.info("research_on_accept_done", prospect_id=pid, version=version, reason=why)
        except SpendCapReached as exc:
            log.warning("research_on_accept_stopped", prospect_id=pid, reason=exc.message)
            return
        except Exception:  # noqa: BLE001 - background work must never raise; the prospect stays pending for the bulk button
            log.exception("research_on_accept_failed", prospect_id=pid)
        finally:
            with _lock:
                _inflight.discard(pid)


def kick_for_enquiries(enquiry_ids: list[int]) -> int:
    """After an accept has been COMMITTED: start research for the prospects of those enquiries that need it. Returns how
    many prospects were started (0 when ``SALES0_AUTO_RESEARCH`` is off). Never raises."""
    if not get_settings().auto_research:
        return 0
    try:
        with session_scope() as db:
            pids = list(dict.fromkeys(
                int(pid) for pid in db.scalars(
                    select(Enquiry.prospect_id).where(Enquiry.id.in_(enquiry_ids), Enquiry.review_status == "accepted",
                                                      Enquiry.prospect_id.is_not(None))
                )
            ))  # fmt: skip
            pids = [pid for pid in pids if reason_for(db, pid) is not None and pid not in _inflight]
        if pids:
            _spawn(lambda: _work(pids))
        return len(pids)
    except Exception:  # noqa: BLE001 - accepting an enquiry must never fail because of research
        log.exception("research_on_accept_kick_failed")
        return 0


__all__ = ["BulkResult", "Outcome", "Overview", "default_fetcher", "in_scope_prospect_ids", "kick_for_enquiries", "overview",
           "reason_for", "run_pending"]
