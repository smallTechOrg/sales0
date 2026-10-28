"""Read-only thread/message metadata for reply tracking.

Only headers + Gmail's own snippet are fetched (``format=metadata``): the full body of a
reply is never downloaded by the adapter, so nothing larger than a snippet can be stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any

# Headers requested from Gmail (format=metadata). Everything the classifier needs, nothing more.
METADATA_HEADERS: tuple[str, ...] = (
    "From", "To", "Cc", "Subject", "Date", "Message-ID", "In-Reply-To", "References",
    "Auto-Submitted", "X-Autoreply", "X-Autorespond", "X-Auto-Response-Suppress", "Precedence",
    "Content-Type", "X-Failed-Recipients", "Return-Path", "List-Id",
)  # fmt: skip


@dataclass(frozen=True)
class ThreadMessage:
    id: str
    thread_id: str
    from_addr: str  # lowercased bare address ("" when unparseable)
    from_name: str
    to_addrs: tuple[str, ...]
    cc_addrs: tuple[str, ...]
    subject: str
    date: datetime  # UTC
    snippet: str
    labels: tuple[str, ...]
    message_id_header: str = ""
    in_reply_to: str = ""
    references: str = ""
    auto_submitted: str = ""
    x_autoreply: str = ""
    precedence: str = ""
    content_type: str = ""
    x_failed_recipients: str = ""
    return_path: str = ""
    headers: dict[str, str] = field(default_factory=dict, compare=False)

    @property
    def is_sent(self) -> bool:
        return "SENT" in self.labels

    @property
    def is_draft(self) -> bool:
        return "DRAFT" in self.labels


def _addrs(value: str) -> tuple[str, ...]:
    return tuple(a.lower() for _, a in getaddresses([value]) if a)


def _date(header: str, internal_ms: str | None) -> datetime:
    if internal_ms and str(internal_ms).isdigit():
        return datetime.fromtimestamp(int(internal_ms) / 1000, UTC)
    try:
        parsed = parsedate_to_datetime(header)
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return datetime.fromtimestamp(0, UTC)


def parse_message(raw: dict[str, Any]) -> ThreadMessage:
    """One Gmail ``users.messages`` resource (format=metadata) -> ThreadMessage."""
    headers: dict[str, str] = {}
    for h in (raw.get("payload") or {}).get("headers") or []:
        name = str(h.get("name", "")).lower()
        if name and name not in headers:
            headers[name] = str(h.get("value", ""))
    name, addr = parseaddr(headers.get("from", ""))
    return ThreadMessage(
        id=str(raw["id"]),
        thread_id=str(raw.get("threadId", "")),
        from_addr=addr.lower(),
        from_name=name,
        to_addrs=_addrs(headers.get("to", "")),
        cc_addrs=_addrs(headers.get("cc", "")),
        subject=headers.get("subject", ""),
        date=_date(headers.get("date", ""), raw.get("internalDate")),
        snippet=str(raw.get("snippet", "")),
        labels=tuple(str(x) for x in raw.get("labelIds") or []),
        message_id_header=headers.get("message-id", ""),
        in_reply_to=headers.get("in-reply-to", ""),
        references=headers.get("references", ""),
        auto_submitted=headers.get("auto-submitted", ""),
        x_autoreply=(headers.get("x-autoreply") or headers.get("x-autorespond") or ""),
        precedence=headers.get("precedence", ""),
        content_type=headers.get("content-type", ""),
        x_failed_recipients=headers.get("x-failed-recipients", ""),
        return_path=headers.get("return-path", ""),
        headers=headers,
    )
