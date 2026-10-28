"""Reply-tracking fixtures: the outreach team (fake Gmail per member) plus SENT messages with threads."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import OutreachMessage, OutreachReply, TeamMember
from sales0.integrations.gmail.threads import ThreadMessage
from tests.integration.outreach import conftest as _outreach
from tests.integration.outreach.conftest import Team, make_message

# Re-export the outreach fixtures (migrated SQLite + two members with fake Gmail clients).
db = _outreach.db
team = _outreach.team

_ids = itertools.count(1)


def sent_message(
    db: Session, team: Team, member: TeamMember, *, to: str = "p@acme.example", cc: str | None = None, sent_at: datetime | None = None,
    subject: str = "Workshop for Acme",
) -> OutreachMessage:
    """A SENT outreach message whose thread (just the SENT message) exists in the member's fake mailbox."""
    n = next(_ids)
    when = sent_at or (datetime.now(UTC) - timedelta(hours=2))
    msg = make_message(db, member, to=to, subject=subject, state="sent")
    msg.cc_addrs = cc or ""
    msg.gmail_message_id, msg.gmail_thread_id, msg.sent_at = f"sent-{n}", f"thr-{n}", when
    db.commit()
    team.gmail[member.id].threads[msg.gmail_thread_id] = [
        ThreadMessage(
            id=msg.gmail_message_id, thread_id=msg.gmail_thread_id, from_addr=member.email.lower(), from_name=member.name,
            to_addrs=(to.lower(),), cc_addrs=(), subject=subject, date=when, snippet="Hello ...", labels=("SENT",),
        )
    ]
    return msg


def reply(team: Team, member: TeamMember, msg: OutreachMessage, from_addr: str = "p@acme.example", **kw):  # type: ignore[no-untyped-def]
    return team.gmail[member.id].add_reply(msg.gmail_thread_id or "", from_addr, **kw)


def all_replies(db: Session) -> list[OutreachReply]:
    db.expire_all()
    return list(db.scalars(select(OutreachReply).order_by(OutreachReply.id)))


def fresh(db: Session, msg: OutreachMessage) -> OutreachMessage:
    db.expire_all()
    return db.get(OutreachMessage, msg.id)  # type: ignore[return-value]
