"""q29 (SMA-56): ``contacts.location`` - where a person is (city / region / country), next to their LinkedIn profile.

Revision ID: q29_contact_location_3e7a9b21
Revises: q28_ai_provider_8d2f6b14
Create Date: 2026-11-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q29_contact_location_3e7a9b21"
down_revision: str | Sequence[str] | None = "q28_ai_provider_8d2f6b14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("contacts") as batch:
        batch.add_column(sa.Column("location", sa.String(120), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("contacts") as batch:
        batch.drop_column("location")
