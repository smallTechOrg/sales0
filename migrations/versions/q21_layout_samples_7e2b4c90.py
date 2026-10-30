"""Layout samples: a cleaned copy of a keyword's result cards when it ended with a layout warning
(``scrape_run_keywords.layout_sample_json``, nullable Text). The run page offers it as a download.

Revision ID: q21_layout_samples_7e2b4c90
Revises: q20_keyword_offering_4c7a9e12
Create Date: 2026-10-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q21_layout_samples_7e2b4c90"
down_revision: str | Sequence[str] | None = "q20_keyword_offering_4c7a9e12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("scrape_run_keywords") as batch:
        batch.add_column(sa.Column("layout_sample_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scrape_run_keywords") as batch:
        batch.drop_column("layout_sample_json")
