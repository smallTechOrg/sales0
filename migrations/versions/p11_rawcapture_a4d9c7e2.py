"""Maximum raw capture: ``seen_posts`` keeps what the scraper parsed but used to drop (``posted_date``, ``truncated``,
``link_kind``, ``search_url``) plus ``raw_card`` (JSON: the card's innerText, reaction / comment / repost counts, links,
media counts, capped outer HTML...). Scraping is the expensive step, so everything cheap to read is stored once.

Plain nullable ADD COLUMN (no table rebuild), retry-safe (added only when missing). Existing rows stay NULL: those
facts were never captured for them.

Revision ID: p11_rawcapture_a4d9c7e2
Revises: p10_worker_e6c2b9d4
Create Date: 2026-10-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p11_rawcapture_a4d9c7e2"
down_revision: str | Sequence[str] | None = "p10_worker_e6c2b9d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    ("posted_date", lambda: sa.Column("posted_date", sa.Date(), nullable=True)),
    ("truncated", lambda: sa.Column("truncated", sa.Boolean(), nullable=True)),
    ("link_kind", lambda: sa.Column("link_kind", sa.String(10), nullable=True)),
    ("search_url", lambda: sa.Column("search_url", sa.String(2000), nullable=True)),
    ("raw_card", lambda: sa.Column("raw_card", sa.JSON(), nullable=True)),
)


def _have() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("seen_posts")}


def upgrade() -> None:
    have = _have()
    for name, make in COLUMNS:
        if name not in have:
            op.add_column("seen_posts", make())


def downgrade() -> None:
    have = _have()
    for name, _ in reversed(COLUMNS):
        if name in have:
            op.drop_column("seen_posts", name)
