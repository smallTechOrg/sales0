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


def startup_backfill(db: Session) -> None:
    """Once per DB (app_meta ``crm_flags_backfilled_v1``): recompute flags for the existing data."""
    from sales0.db.models import AppMeta
    from sales0.services.crm import flags

    if db.get(AppMeta, BACKFILL_KEY) is not None:
        return
    flags.recompute_all(db)
    db.add(AppMeta(key=BACKFILL_KEY, value="1"))
    db.flush()


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    """``from sales0.services.crm import UNSET`` convenience."""
    if name == "UNSET":
        from sales0.services.crm._common import UNSET

        return UNSET
    raise AttributeError(name)


__all__ = ["BACKFILL_KEY", "install", "startup_backfill"]
