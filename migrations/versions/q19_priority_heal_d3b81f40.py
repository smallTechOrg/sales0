"""Heal prospect priorities: a derived (not hand-set) priority that is still empty gets the highest priority among the
prospect's linked, not-discarded enquiries. Prospects whose enquiries were linked after the q16 backfill (or by a path
that did not recompute it) showed no priority chip on the Prospects tab although their enquiries had one.

Revision ID: q19_priority_heal_d3b81f40
Revises: q18_split_unnamed_c1a47e3b
Create Date: 2026-10-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q19_priority_heal_d3b81f40"
down_revision: str | Sequence[str] | None = "q18_split_unnamed_c1a47e3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RANK = {"high": 3, "medium": 2, "low": 1}


def upgrade() -> None:
    bind = op.get_bind()
    prospects = sa.table("prospects", sa.column("id", sa.Integer), sa.column("priority", sa.String), sa.column("priority_source", sa.String))
    enquiries = sa.table("enquiries", sa.column("prospect_id", sa.Integer), sa.column("priority", sa.String), sa.column("review_status", sa.String))
    best: dict[int, str] = {}
    for pid, prio in bind.execute(sa.select(enquiries.c.prospect_id, enquiries.c.priority).where(
            enquiries.c.prospect_id.is_not(None), enquiries.c.priority.is_not(None), enquiries.c.review_status != "discarded")):
        if prio in RANK and RANK[prio] > RANK.get(best.get(pid, ""), 0):
            best[pid] = prio
    for pid, prio in best.items():
        bind.execute(sa.update(prospects).where(
            prospects.c.id == pid, prospects.c.priority.is_(None), prospects.c.priority_source != "manual").values(priority=prio, priority_source="derived"))


def downgrade() -> None:
    """Data-only heal: nothing to undo."""
