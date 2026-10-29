"""Digest fixtures: migrated SQLite DB, team members with fake Gmail clients, ORM seeders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

from sqlalchemy.orm import Session

from sales0.db.models import Enquiry, LlmCall, OutreachMessage, ScrapeRun, TeamMember
from tests.integration.outreach.conftest import Team, db, team  # noqa: F401  (fixtures re-exported)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
_n = count(1)


def hours_ago(h: float) -> datetime:
    return NOW - timedelta(hours=h)


def add_enquiry(db: Session, *, added: datetime, verdict: str | None = "genuine", triage: str = "done", priority: str = "high", review: str = "pending_review", company: str = "Acme", summary: str = "GenAI workshop for 40 managers") -> Enquiry:  # noqa: E501
    n = next(_n)
    e = Enquiry(
        content_hash=f"hash-{n:040d}", raw_text=f"post {n}", date_added=added, ai_verdict=verdict, triage_status=triage,
        priority=priority, review_status=review, company=company, requirement_summary=summary,
    )  # fmt: skip
    db.add(e)
    db.commit()
    return e


def add_message(db: Session, member: TeamMember, *, state: str = "draft", created: datetime | None = None, **fields: Any) -> OutreachMessage:  # noqa: E501
    created = created or hours_ago(100)
    msg = OutreachMessage(
        member_id=member.id, connection_email=member.email, from_addr=member.email, to_addrs="p@acme.example",
        subject=fields.pop("subject", "Workshop for Acme"), body="Hello", state=state, created_at=created,
        updated_at=fields.pop("updated_at", created), **fields,
    )  # fmt: skip
    db.add(msg)
    db.commit()
    return msg


def add_run(db: Session, *, started: datetime, status: str = "succeeded", posts_new: int = 0, errors: int = 0, abort: str | None = None) -> ScrapeRun:  # noqa: E501
    run = ScrapeRun(
        trigger="manual", mode="live", status=status, started_at=started, finished_at=started + timedelta(minutes=5),
        posts_new=posts_new, errors_count=errors, abort_reason=abort,
    )  # fmt: skip
    db.add(run)
    db.commit()
    return run


def add_llm_call(db: Session, *, micros: int, at: datetime, feature: str = "triage") -> None:
    db.add(LlmCall(feature=feature, model="claude-haiku-5-5", status="ok", cost_usd_micros=micros, created_at=at))
    db.commit()


__all__ = ["NOW", "Team", "add_enquiry", "add_llm_call", "add_message", "add_run", "db", "hours_ago", "team"]
