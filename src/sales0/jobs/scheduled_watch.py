"""THE one place the scheduler touches the prospect watch (adapter over ``tracked_profiles.start_watch_run``).

Overlap with a discovery run or any other browser job, the safety cool-down, a LinkedIn session that
needs attention, the daily post cap and "nothing due" are all enforced by the run-start service, so a
scheduled start can never overlap a manual run. Extra rule here: the monthly AI spend cap.
"""

from __future__ import annotations

from sales0.db.engine import session_scope
from sales0.errors import DomainError
from sales0.jobs.scheduled_discovery import DiscoveryStart


def start_scheduled_watch(linkedin_account_id: int | None = None) -> DiscoveryStart:
    from sales0.services import runs as runs_service
    from sales0.services import tracked_profiles
    from sales0.services.costs_report import spend_cap_status

    try:
        with session_scope() as db:
            if spend_cap_status(db).state == "exceeded":
                return DiscoveryStart(False, code="spend_cap_reached", reason="Monthly AI spend cap reached - not starting a watch run that could not be triaged.")
            try:
                view = tracked_profiles.start_watch_run(db, origin="scheduled", linkedin_account_id=linkedin_account_id)
            except DomainError as exc:
                retry_at = runs_service.safety_cooldown_until(db, account_id=linkedin_account_id) if exc.code == "safety_cooldown" else None
                return DiscoveryStart(False, code=exc.code, reason=exc.message, retry_at=retry_at)
            return DiscoveryStart(True, run_id=view.id)
    except Exception as exc:  # noqa: BLE001 - the scheduler must survive any start failure
        return DiscoveryStart(False, code="start_failed", reason=f"{type(exc).__name__}: {str(exc)[:200]}")
