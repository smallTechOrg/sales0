"""CRM memory (Phase 2). Step-0 placeholder: S1 replaces this file wholesale."""

from __future__ import annotations

from sqlalchemy.orm import Session


def install() -> None:
    """Subscribe the CRM hooks to the domain events (idempotent). No-op until S1 adds the hooks."""


def startup_backfill(db: Session) -> None:
    """Once per DB (app_meta ``crm_flags_backfilled_v1``): recompute flags. No-op until S1."""


__all__ = ["install", "startup_backfill"]
