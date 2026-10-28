"""Builders for CRM tests (plain ORM rows; no LLM involved)."""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from sales0.db.models import Enquiry, LinkedinProfile, OutreachMessage, OutreachReply, TeamMember

_ids = itertools.count(1)
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def member(db: Session, name: str = "Priya", email: str | None = None) -> TeamMember:
    n = next(_ids)
    m = TeamMember(name=name, email=email or f"{name.lower()}{n}@team.example", role="member")
    db.add(m)
    db.flush()
    return m


def profile(db: Session, slug: str = "in/acme-hr", name: str = "Acme HR") -> LinkedinProfile:
    p = LinkedinProfile(slug=slug, kind="person", profile_url=f"https://www.linkedin.com/{slug}/", display_name=name,
                        first_seen_at=T0, last_seen_at=T0)
    db.add(p)
    db.flush()
    return p


def enquiry(db: Session, *, status: str = "accepted", profile_row: LinkedinProfile | None = None, **kw: object) -> Enquiry:
    n = next(_ids)
    values: dict[str, object] = {
        "content_hash": f"{n:064d}",
        "raw_text": kw.pop("raw_text", f"We need AI training for our managers, post {n}."),
        "review_status": status,
        "stage": "prospect_new" if status == "accepted" else None,
        "triage_status": "done",
        "author_name": "Asha Rao",
        "date_added": T0,
        "requirement_summary": f"Acme Training - AI workshop {n} - Pune | Online",
    }
    if profile_row is not None:
        values["author_profile_id"] = profile_row.id
        values["author_profile_url"] = profile_row.profile_url
    values.update(kw)
    e = Enquiry(**values)
    db.add(e)
    db.flush()
    return e


def sent_message(db: Session, m: TeamMember, *, enquiry_row: Enquiry | None = None, to: str = "asha@acme.example",
                 state: str = "sent", sent_at: datetime | None = None, **kw: object) -> OutreachMessage:
    msg = OutreachMessage(
        member_id=m.id, connection_email=m.email, from_addr=m.email, to_addrs=to, subject="Corporate AI training for Acme",
        body="Hello", state=state, sent_at=sent_at or (T0 if state == "sent" else None), enquiry_id=enquiry_row.id if enquiry_row else None,
        **kw,
    )
    db.add(msg)
    db.flush()
    return msg


def reply(db: Session, msg: OutreachMessage, *, kind: str = "human", intent: str | None = "interested",
          at: datetime | None = None) -> OutreachReply:
    n = next(_ids)
    r = OutreachReply(
        message_id=msg.id, member_id=msg.member_id, gmail_message_id=f"g{n}", gmail_thread_id="t", from_addr="asha@acme.example",
        received_at=at or (T0 + timedelta(days=1)), snippet="Sounds good", kind=kind, intent=intent,
    )
    db.add(r)
    db.flush()
    return r


def day(n: int) -> date:
    return (T0 + timedelta(days=n)).date()
