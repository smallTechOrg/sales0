"""Reply tracking: inbound replies to sent outreach, the suppression list, per-member poll state.

``outreach_replies.gmail_message_id`` is UNIQUE, which makes polling idempotent. Only a
snippet (<= 500 chars) is stored, never a full body. ``outreach_messages.reply_status`` is
the cheap denormalised flag the outbox badge reads.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc

REPLY_KINDS = ("human", "auto_reply", "bounce")
REPLY_INTENTS = ("interested", "not_now", "unsubscribe", "question", "other")
REPLY_STATUSES = ("none", "replied", "auto_reply", "bounced")
SUPPRESSION_REASONS = ("unsubscribe", "bounce", "manual")


class OutreachReply(Base):
    __tablename__ = "outreach_replies"
    __table_args__ = (
        in_check("kind", REPLY_KINDS, "kind"),
        in_check("intent", REPLY_INTENTS, "intent", nullable=True),
        Index("ix_outreach_replies_handled_received_at", "handled", "received_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("outreach_messages.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("team_members.id", ondelete="CASCADE"), nullable=False, index=True)
    gmail_message_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    gmail_thread_id: Mapped[str] = mapped_column(String(120), nullable=False)
    from_addr: Mapped[str] = mapped_column(String(254), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    snippet: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default="")
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    intent: Mapped[str | None] = mapped_column(String(12))
    reason: Mapped[str | None] = mapped_column(String(300))  # one line: why this kind/intent
    classified_by: Mapped[str] = mapped_column(String(10), nullable=False, default="headers", server_default="headers")
    handled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    handled_by_member_id: Mapped[int | None] = mapped_column(Integer)
    handled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)


class Suppression(Base):
    """An address we must never draft or send to (unsubscribe / hard bounce / manual)."""

    __tablename__ = "suppressions"
    __table_args__ = (in_check("reason", SUPPRESSION_REASONS, "reason"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)  # lowercased
    reason: Mapped[str] = mapped_column(String(12), nullable=False)
    source_reply_id: Mapped[int | None] = mapped_column(ForeignKey("outreach_replies.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)


class ReplyPollState(Base):
    """Per-member poller health and backoff."""

    __tablename__ = "reply_poll_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("team_members.id", ondelete="CASCADE"), nullable=False, unique=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_new_replies: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


__all__ = [
    "REPLY_INTENTS",
    "REPLY_KINDS",
    "REPLY_STATUSES",
    "SUPPRESSION_REASONS",
    "OutreachReply",
    "ReplyPollState",
    "Suppression",
]
