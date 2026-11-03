"""Tags the AI applies to a company after research / a summary refresh (``AccountSummary.relationship`` /
``fit_offerings``):

- the characteristic **Partner** (system key ``partner``) or **Direct client** (``direct_client``) on the company's
  prospect - only when it has neither yet (a person's choice is kept);
- the company's offerings - only when it has none yet.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import Characteristic, EntityCharacteristic, Prospect
from sales0.services.crm import company_offerings
from sales0.services.crm.actor import SYSTEM, Actor

RELATIONSHIP_KEYS = {"partner": "partner", "direct_client": "direct_client"}


def apply(db: Session, prospect_id: int, summary: Any, *, actor: Actor = SYSTEM, now: datetime | None = None) -> dict[str, Any]:
    from sales0.services.crm import characteristics

    out: dict[str, Any] = {"characteristic": None, "offerings": []}
    p = db.get(Prospect, prospect_id)
    if p is None or summary is None:
        return out
    ids = {k: i for k, i in db.execute(select(Characteristic.system_key, Characteristic.id)
                                       .where(Characteristic.system_key.in_(list(RELATIONSHIP_KEYS.values()))))}
    key = RELATIONSHIP_KEYS.get(getattr(summary, "relationship", "unknown"))
    if key and key in ids:
        has = set(db.scalars(select(EntityCharacteristic.characteristic_id).where(
            EntityCharacteristic.entity_type == "prospect", EntityCharacteristic.entity_id == prospect_id)))
        if not has & set(ids.values()):
            characteristics.add_to(db, "prospect", prospect_id, [ids[key]], actor=actor, now=now)
            out["characteristic"] = key
    if p.company_id:
        out["offerings"] = company_offerings.add_if_untagged(db, p.company_id, getattr(summary, "fit_offerings", []) or [])
    return out


__all__ = ["apply"]
