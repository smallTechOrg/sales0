"""Tiny in-process domain-event dispatcher with a subscriber registry.

The CRM (Phase 2) will subscribe to ``ReplyReceived`` to move prospects to leads without the
reply poller knowing about it. Handlers run synchronously, in registration order, inside the
publisher's DB session; a failing handler is logged and never stops the others or the publisher.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from sales0.logging import get_logger

log = get_logger("sales0.events")


@dataclass(frozen=True)
class ReplyReceived:
    outreach_message_id: int
    kind: str  # human | auto_reply | bounce
    intent: str | None  # interested | not_now | unsubscribe | question | other | None
    reply_id: int | None = None
    member_id: int | None = None
    from_addr: str = ""
    hard_bounce: bool = True  # only meaningful for kind == "bounce"
    failed_recipients: tuple[str, ...] = ()


@dataclass(frozen=True)
class MessageSent:
    """Published by ``outreach.messages._execute`` after ``state='sent'``, before the commit."""

    outreach_message_id: int
    member_id: int
    to_addrs: tuple[str, ...]
    sent_at: datetime


@dataclass(frozen=True)
class EnquiryDecided:
    """Published by ``inbox.decide`` once per enquiry, after the row is updated and flushed."""

    enquiry_id: int
    decision: str  # "accept" | "discard"
    previous_review_status: str
    actor_user_id: int | None = None
    actor_member_id: int | None = None


Handler = Callable[[Session, Any], None]
_lock = threading.Lock()
_subscribers: dict[type, list[Handler]] = {}


def subscribe(event_type: type, handler: Handler) -> None:
    """Register ``handler(db, event)``; registering the same function twice is a no-op."""
    with _lock:
        handlers = _subscribers.setdefault(event_type, [])
        if handler not in handlers:
            handlers.append(handler)


def unsubscribe(event_type: type, handler: Handler) -> None:
    with _lock:
        if handler in _subscribers.get(event_type, []):
            _subscribers[event_type].remove(handler)


def publish(db: Session, event: object) -> int:
    """Deliver ``event`` to its subscribers. Returns how many handlers ran without error."""
    with _lock:
        handlers = list(_subscribers.get(type(event), []))
    ok = 0
    for handler in handlers:
        try:
            with db.begin_nested():
                handler(db, event)
            ok += 1
        except Exception:  # noqa: BLE001 - one subscriber must not break the others
            log.exception("event_handler_failed", event_type=type(event).__name__, handler=getattr(handler, "__name__", "?"))
    return ok


def subscribers(event_type: type) -> list[Handler]:
    with _lock:
        return list(_subscribers.get(event_type, []))


def reset() -> None:
    """Test seam: drop every subscriber (the reply service re-adds its defaults on next use)."""
    with _lock:
        _subscribers.clear()


__all__ = ["EnquiryDecided", "MessageSent", "ReplyReceived", "publish", "reset", "subscribe", "subscribers", "unsubscribe"]
