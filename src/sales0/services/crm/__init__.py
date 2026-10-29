"""CRM memory (Phase 2): prospects, leads, clients, contacts, timeline, follow-ups, flags, registries.

``install()`` subscribes the hooks to the domain events; ``startup_backfill`` runs once per DB.
Submodules are imported lazily so that ``from sales0.services.crm.actor import Actor`` stays cheap
and cycle-free for the sheet-sync mapping and the importer.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

BACKFILL_KEY = "crm_flags_backfilled_v1"


def install() -> None:
    """Subscribe the CRM hooks to the domain events (idempotent)."""
    from sales0.services import events
    from sales0.services.crm import hooks

    events.subscribe(events.MessageSent, hooks.on_message_sent)
    events.subscribe(events.ReplyReceived, hooks.on_reply_received)
    events.subscribe(events.EnquiryDecided, hooks.on_enquiry_decided)
    events.subscribe(events.TrackedProfileHit, hooks.on_tracked_profile_hit)
    # nightly: the retention purge job also heals accepted enquiries whose CRM hook failed (registered here so the
    # scheduler module stays untouched; the hook ignores the retention cutoff)
    from sales0.jobs.scheduler import register_purge_hook

    register_purge_hook("crm_heal_orphan_accepts", lambda db, _cutoff: heal_accepted_without_prospect(db))


def startup_backfill(db: Session) -> None:
    """Every startup: heal accepted enquiries without a prospect. Once per DB (app_meta ``crm_flags_backfilled_v1``):
    recompute flags for the existing data."""
    from sales0.db.models import AppMeta
    from sales0.services.crm import flags

    heal_accepted_without_prospect(db)
    if db.get(AppMeta, BACKFILL_KEY) is not None:
        return
    flags.recompute_all(db)
    db.add(AppMeta(key=BACKFILL_KEY, value="1"))
    db.flush()


def heal_accepted_without_prospect(db: Session, *, limit: int = 200) -> int:
    """Create the missing prospect for every accepted enquiry with ``prospect_id`` NULL (a swallowed hook failure,
    a crash between the decision and the hook, an old DB). Idempotent; one savepoint per enquiry, a failing one is
    audited on its timeline and skipped. Returns how many were healed. Runs at startup and nightly."""
    from sqlalchemy import select

    from sales0.db.models import Enquiry
    from sales0.logging import get_logger
    from sales0.services.crm import prospects, timeline
    from sales0.services.crm.actor import SYSTEM

    log = get_logger("sales0.crm.heal")
    ids = list(
        db.scalars(
            select(Enquiry.id)
            .where(Enquiry.review_status == "accepted", Enquiry.prospect_id.is_(None))
            .order_by(Enquiry.id)
            .limit(limit)
        )
    )
    healed = 0
    for eid in ids:
        try:
            with db.begin_nested():
                view = prospects.ensure_for_enquiry(db, eid, actor=SYSTEM)
                timeline.add_event(
                    db, subject_type="enquiry", subject_id=eid, kind="note", actor=SYSTEM,
                    body="Prospect created by the consistency check (the accept had not produced one).",
                    data={"system": True, "healed": True, "prospect_id": view.id},
                )
            healed += 1
        except Exception as exc:  # noqa: BLE001 - one bad enquiry must not stop the rest
            log.exception("crm_heal_failed", enquiry_id=eid)
            try:
                with db.begin_nested():
                    timeline.add_event(
                        db, subject_type="enquiry", subject_id=eid, kind="note", actor=SYSTEM,
                        body="The consistency check could not create a prospect for this accepted enquiry.",
                        data={"system": True, "heal_failed": True, "error": f"{type(exc).__name__}: {exc}"[:300]},
                    )
            except Exception:  # noqa: BLE001
                pass
    if healed:
        log.info("crm_healed_accepted_without_prospect", count=healed)
    return healed


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    """``from sales0.services.crm import UNSET`` convenience."""
    if name == "UNSET":
        from sales0.services.crm._common import UNSET

        return UNSET
    raise AttributeError(name)


__all__ = ["BACKFILL_KEY", "heal_accepted_without_prospect", "install", "startup_backfill"]
