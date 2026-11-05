"""The Health page: one row per moving part (replies, laptop worker, LinkedIn session, AI, sheet sync, runs, database,
automatic status), each with a state (ok | warn | off | error), a one-line summary and what to do about it.

Read-only; never raises for one broken part (that row says what failed instead)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc
from sales0.config import get_settings
from sales0.logging import get_logger

log = get_logger("sales0.health_report")


@dataclass
class Check:
    key: str
    title: str
    state: str  # ok | warn | off | error
    summary: str
    fix: str = ""
    details: list[str] = field(default_factory=list)


def _ago(value: datetime | None) -> str:
    from sales0.web.templating import ago

    return ago(ensure_utc(value)) if value else "never"


def _replies(db: Session) -> Check:
    from sales0.db.models import GmailConnection, OutreachMessage, ReplyPollState, TeamMember
    from sales0.services import replies

    s = get_settings()
    h = replies.poller_health(db)
    state = {"ok": "ok", "idle": "warn", "warn": "warn", "off": "off"}.get(h.state, "warn")
    fix = ""
    if h.state == "off" and s.is_hosted:
        fix = ("Cloud Run has no background timer, so replies are checked by the laptop worker. Set SALES0_WORKER_TOKEN on "
               "the hosted app and run `sales0 worker` on the laptop (SALES0_WORKER_REPLY_CHECK_MINUTES, default 10).")
    elif h.state == "off":
        fix = "Set SALES0_REPLY_POLL_MINUTES (e.g. 10) and restart, or press Check now on the Replies page."
    elif "worker" in h.label:
        fix = "Start the laptop worker (`sales0 worker`) - it asks for a reply check every few minutes - or press Check now on Replies."
    details: list[str] = []
    senders = set(db.scalars(select(OutreachMessage.member_id).where(OutreachMessage.state == "sent").distinct()))
    conns = {c.member_id: c for c in db.scalars(select(GmailConnection))}
    states = {r.member_id: r for r in db.scalars(select(ReplyPollState))}
    for m in db.scalars(select(TeamMember).where(TeamMember.active.is_(True)).order_by(TeamMember.name)):
        c = conns.get(m.id)
        st = states.get(m.id)
        if c is None:
            gmail = "Gmail not connected"
        else:
            gmail = f"Gmail {c.status.replace('_', ' ')} ({c.email})"
        poll = ""
        if st is not None:
            poll = f"; last check {_ago(st.last_polled_at)}" + (f"; last error: {st.last_error[:120]}" if st.last_error else "")
        sent = "" if m.id in senders else "; has not sent any email yet (nothing to check)"
        details.append(f"{m.name}: {gmail}{poll}{sent}")
        if m.id in senders and (c is None or c.status != "connected"):
            state = "warn" if state == "ok" else state
            fix = fix or f"{m.name} must reconnect Gmail (Team -> Connect Gmail) - only they can grant access."
    return Check("replies", "Replies (Gmail)", state, f"{h.label}. {h.detail}", fix, details)


def _worker(db: Session) -> Check:
    s = get_settings()
    if not s.is_hosted:
        return Check("worker", "Laptop worker", "off", "Not used: this server runs LinkedIn in its own browser (local mode).")
    from sales0.services import worker_service

    w = worker_service.worker_status(db)
    if w.online:
        return Check("worker", "Laptop worker", "ok", f"Online: {w.name or 'worker'} {w.version or ''}, last seen {_ago(w.last_seen_at)}.")
    return Check("worker", "Laptop worker", "warn", f"Offline (last seen {_ago(w.last_seen_at)}); {w.queued_runs} run(s) waiting.",
                 w.offline_hint or "Start `sales0 worker` on the laptop.")


def _linkedin(db: Session) -> Check:
    from sales0.services import session as session_service

    st = session_service.session_status(db)
    label = getattr(st, "label", None) or st.status.replace("_", " ")
    state = "ok" if st.status in ("connected", "ok", "logged_in") else ("error" if st.status == "needs_attention" else "warn")
    return Check("linkedin", "LinkedIn session", state, f"{label}.", "" if state == "ok" else "Setup -> LinkedIn: log in again on the laptop.")


def _ai(db: Session) -> Check:
    from sales0.services import costs_report

    s = get_settings()
    if not s.api_key_present:
        return Check("ai", "AI (Anthropic)", "error", "No API key configured: triage, research and drafting are off.",
                     "Set SALES0_ANTHROPIC_API_KEY on the server.")
    cap = costs_report.spend_cap_status(db)
    spent = cap.month_micros / 1_000_000
    if cap.state == "off":
        return Check("ai", "AI (Anthropic)", "ok", f"Key present; ${spent:.2f} spent this month, no monthly cap.")
    state = {"ok": "ok", "warn": "warn", "exceeded": "error"}.get(cap.state, "warn")
    return Check("ai", "AI (Anthropic)", state, f"Key present; ${spent:.2f} of ${cap.cap_micros / 1_000_000:.2f} this month ({cap.pct:.0f}%).",
                 "Raise SALES0_MONTHLY_SPEND_CAP_USD or wait for next month." if state != "ok" else "")


def _sheet(db: Session) -> Check:
    from sales0.db.models import SheetSyncState

    s = get_settings()
    row = db.get(SheetSyncState, 1)
    if row is None or row.last_finished_at is None:
        return Check("sheet", "Google Sheet sync", "off", "Never synced.", "Sync page -> Sync now (it is a button: no timer on Cloud Run)." if s.is_hosted else "")
    state = "ok" if row.last_status in ("ok", "done", "success") else "warn"
    return Check("sheet", "Google Sheet sync", state, f"Last sync {_ago(row.last_finished_at)}: {row.last_status or '?'}"
                 + (f" - {row.last_error[:160]}" if row.last_error else "") + ".")


def _runs(db: Session, now: datetime) -> Check:
    from sales0.db.models import ScrapeRun

    last = db.scalar(select(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(1))
    if last is None:
        return Check("runs", "LinkedIn search runs", "off", "No run yet.", "Runs -> Run now.")
    when = last.finished_at or last.started_at
    stale = when is not None and now - ensure_utc(when) > timedelta(days=3)
    bad = last.status in ("failed", "aborted")
    state = "error" if bad else ("warn" if stale else "ok")
    return Check("runs", "LinkedIn search runs", state, f"Last run #{last.id}: {last.status.replace('_', ' ')}, {_ago(when)}.",
                 "Open Runs to see why." if bad else ("No run for 3+ days - Runs -> Run now." if stale else ""))


def _database(db: Session) -> Check:
    from sales0.db.engine import get_engine
    from sales0.db.migrate_check import current_revision, head_revision

    cur, head = current_revision(get_engine()), head_revision()
    if cur == head:
        return Check("database", "Database", "ok", f"Schema up to date ({cur}).")
    return Check("database", "Database", "error", f"Schema at {cur}, code expects {head}.", "Run the migration (`alembic upgrade head`) / redeploy.")


def _auto_status(db: Session) -> Check:
    from sales0.db.models import TimelineEvent

    s = get_settings()
    if not s.auto_status:
        return Check("auto_status", "Automatic company status", "off", "Off: statuses only move by hand.", "Set SALES0_AUTO_STATUS=true.")
    last = db.scalar(select(func.max(TimelineEvent.occurred_at)).where(
        TimelineEvent.kind == "stage_change", TimelineEvent.subject_type == "company", TimelineEvent.body.like("Status updated automatically%")))
    return Check("auto_status", "Automatic company status", "ok", f"On; last automatic change {_ago(last)}.")


def report(db: Session, now: datetime | None = None) -> list[Check]:
    stamp = now or datetime.now(UTC)
    out: list[Check] = []
    for key, title, fn in (("replies", "Replies (Gmail)", lambda: _replies(db)), ("worker", "Laptop worker", lambda: _worker(db)),
                           ("linkedin", "LinkedIn session", lambda: _linkedin(db)), ("ai", "AI (Anthropic)", lambda: _ai(db)),
                           ("runs", "LinkedIn search runs", lambda: _runs(db, stamp)), ("sheet", "Google Sheet sync", lambda: _sheet(db)),
                           ("auto_status", "Automatic company status", lambda: _auto_status(db)), ("database", "Database", lambda: _database(db))):
        try:
            out.append(fn())
        except Exception as exc:  # noqa: BLE001 - one broken part must not hide the others
            log.warning("health_check_failed", check=key, error=f"{type(exc).__name__}: {exc}"[:200])
            out.append(Check(key, title, "error", f"Could not check: {type(exc).__name__}."))
    return out


__all__ = ["Check", "report"]
