"""Who made a change: a team member's name, ``system``, ``sheet-sync``, ``import`` or ``team``."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from sales0.db.models import TeamMember


@dataclass(frozen=True)
class Actor:
    label: str
    member_id: int | None = None
    by_member_id: int | None = None  # the real logged-in user when an admin acts as someone else

    @staticmethod
    def of_member(m: TeamMember | None) -> Actor:
        """The member's actor. An admin acting as another member (``m.acted_by``) yields "Admin as Member":
        credit goes to the member (``member_id``), the real user is kept in ``by_member_id`` and the label."""
        if m is None:
            return TEAM
        by = getattr(m, "acted_by", None)
        if by is not None and by.id != m.id:
            return Actor(f"{by.name} as {m.name}"[:80], m.id, by.id)
        return Actor(m.name, m.id)


def actor_for(db: Session, member_id: int | None, by_member_id: int | None = None) -> Actor:
    """The actor for a member id; an admin acting as another member yields "Admin as Member" (credit to the member)."""
    m = db.get(TeamMember, member_id) if member_id else None
    if m is not None and by_member_id and by_member_id != m.id:
        by = db.get(TeamMember, by_member_id)
        if by is not None:
            return Actor(f"{by.name} as {m.name}"[:80], m.id, by.id)
    return Actor.of_member(m)


SYSTEM = Actor("system")
SHEET_SYNC = Actor("sheet-sync")
IMPORT = Actor("import")
TEAM = Actor("team")

__all__ = ["actor_for", "IMPORT", "SHEET_SYNC", "SYSTEM", "TEAM", "Actor"]
