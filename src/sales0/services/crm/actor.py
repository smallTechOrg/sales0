"""Who made a change: a team member's name, ``system``, ``sheet-sync``, ``import`` or ``team``."""

from __future__ import annotations

from dataclasses import dataclass

from sales0.db.models import TeamMember


@dataclass(frozen=True)
class Actor:
    label: str
    member_id: int | None = None

    @staticmethod
    def of_member(m: TeamMember | None) -> Actor:
        if m is None:
            return TEAM
        return Actor(m.name, m.id)


SYSTEM = Actor("system")
SHEET_SYNC = Actor("sheet-sync")
IMPORT = Actor("import")
TEAM = Actor("team")

__all__ = ["IMPORT", "SHEET_SYNC", "SYSTEM", "TEAM", "Actor"]
