"""Is the AI working? The Anthropic adapter notes every call's outcome here (in this process), so the app can say
"AI unavailable: <why>" the moment a call fails, with the provider's own reason (e.g. "credit balance is too low")."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class LastFailure:
    at: datetime
    error_type: str
    message: str
    feature: str


_lock = threading.Lock()
_last_failure: LastFailure | None = None
_last_ok: datetime | None = None


def note_failure(error_type: str, message: str, feature: str) -> None:
    global _last_failure
    with _lock:
        _last_failure = LastFailure(datetime.now(UTC), error_type, (message or "")[:300], feature)


def note_success() -> None:
    global _last_ok
    with _lock:
        _last_ok = datetime.now(UTC)


def snapshot() -> tuple[LastFailure | None, datetime | None]:
    with _lock:
        return _last_failure, _last_ok


def reset() -> None:
    """Test seam."""
    global _last_failure, _last_ok
    with _lock:
        _last_failure, _last_ok = None, None


__all__ = ["LastFailure", "note_failure", "note_success", "reset", "snapshot"]
