"""q26: "Client" becomes "Customer" (SMA-16): the two seeded characteristics are renamed ("Direct client" -> "Direct
customer", "Outreach client" -> "Outreach customer"). Only a row that still has its seeded name is touched (a team that
renamed it keeps its own wording), and never into a name that already exists. System keys stay as they are.

Revision ID: q26_customer_wording_6f1a3c58
Revises: q25_manual_moves_2b7d4e90
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q26_customer_wording_6f1a3c58"
down_revision: str | Sequence[str] | None = "q25_manual_moves_2b7d4e90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RENAMES = (("Direct client", "Direct customer"), ("Outreach client", "Outreach customer"))


def _rename(pairs: Sequence[tuple[str, str]]) -> None:
    bind = op.get_bind()
    chars = sa.table("characteristics", sa.column("id", sa.Integer), sa.column("name", sa.String), sa.column("name_key", sa.String))
    for old, new in pairs:
        row = bind.execute(sa.select(chars.c.id).where(chars.c.name == old)).first()
        taken = bind.execute(sa.select(chars.c.id).where(chars.c.name_key == new.lower())).first()
        if row is not None and taken is None:
            bind.execute(chars.update().where(chars.c.id == row[0]).values(name=new, name_key=new.lower()))


def upgrade() -> None:
    _rename(RENAMES)


def downgrade() -> None:
    _rename(tuple((new, old) for old, new in RENAMES))
