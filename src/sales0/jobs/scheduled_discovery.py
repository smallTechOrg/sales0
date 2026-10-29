"""THE one place the scheduler touches discovery.

``start_scheduled_discovery()`` is a thin adapter over the existing public run-start service
(``sales0.services.runs.start_run``). Phase 1.4 rewrites the discovery internals; as long as
``start_run(db, actor_user_id)`` keeps raising ``DomainError`` for "cannot run now" and
returning a view with ``.id`` on success, nothing else in the scheduler needs to change.
If that entrypoint is renamed, edit ONLY this file.

Overlap, daily run/post caps, the safety cool-down, a LinkedIn session that needs attention
and "a run is already active" are all enforced by ``start_run`` itself, so a scheduled start
can never overlap a manual run. The one extra rule added here is the monthly AI spend cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sales0.db.engine import session_scope
from sales0.errors import DomainError

# Skip reasons worth retrying later inside the window (transient), by DomainError code.
RETRYABLE_CODES = frozenset({"run_in_progress", "job_busy", "safety_cooldown"})


@dataclass(frozen=True)
class DiscoveryStart:
    started: bool
    run_id: int | None = None
    code: str | None = None  # DomainError code when not started
    reason: str | None = None
    retry_at: datetime | None = None  # when the reason is expected to clear (cool-down end)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


def start_scheduled_discovery(linkedin_account_id: int | None = None) -> DiscoveryStart:
    """Start a scheduled run on one LinkedIn account (default: the first active account)."""
    from sales0.services import runs as runs_service
    from sales0.services.costs_report import spend_cap_status

    try:
        with session_scope() as db:
            if spend_cap_status(db).state == "exceeded":
                return DiscoveryStart(False, code="spend_cap_reached", reason="Monthly AI spend cap reached - not starting a run that could not be triaged.")
            try:
                view = runs_service.start_run(db, None, linkedin_account_id)
            except DomainError as exc:
                retry_at = runs_service.safety_cooldown_until(db, account_id=linkedin_account_id) if exc.code == "safety_cooldown" else None
                return DiscoveryStart(False, code=exc.code, reason=exc.message, retry_at=retry_at)
            return DiscoveryStart(True, run_id=view.id)
    except Exception as exc:  # noqa: BLE001 - the scheduler must survive any start failure
        return DiscoveryStart(False, code="start_failed", reason=f"{type(exc).__name__}: {str(exc)[:200]}")
