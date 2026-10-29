"""Deck storage metadata + follow-up channel.

* ``decks.storage`` (varchar(10), default ``local``): which store holds the PDF - ``local`` (``<data_dir>/<stored_path>``)
  or ``gcs`` (bucket ``SALES0_ATTACHMENTS_BUCKET``, object ``<stored_path>`` = ``decks/<sha256>.pdf``). The bytes are
  never in the database. Existing rows are ``local``.
* ``decks.content_missing`` (bool, default FALSE): backfilled TRUE for every existing row whose file is not at
  ``<SALES0_DATA_DIR>/<stored_path>`` with the right sha256 (on the hosted app that is all of them: its ``/tmp`` was
  wiped), so the UI says "file missing - upload it again" instead of failing at send time.
* ``follow_ups.channel`` (varchar(10), nullable): how a follow-up was done (whatsapp / linkedin / email / call).

Plain ADD COLUMN (no table rebuild), retry-safe (each column added only when missing). Downgrade drops the columns.

Revision ID: q13_followch_3e7a6c58
Revises: p11_rawcapture_a4d9c7e2
Create Date: 2026-10-29
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "q13_followch_3e7a6c58"
down_revision: str | Sequence[str] | None = "p11_rawcapture_a4d9c7e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _have(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _data_path() -> Path | None:
    try:
        from sales0.config import get_settings

        return get_settings().data_path
    except Exception:  # noqa: BLE001 - no usable settings: every legacy row is flagged missing instead
        return None


def _backfill() -> None:
    bind = op.get_bind()
    decks = sa.table(
        "decks", sa.column("id", sa.Integer), sa.column("stored_path", sa.String), sa.column("sha256", sa.String),
        sa.column("storage", sa.String), sa.column("content_missing", sa.Boolean),
    )  # fmt: skip
    rows = bind.execute(sa.select(decks.c.id, decks.c.stored_path, decks.c.sha256).where(decks.c.storage == "local")).all()
    base = _data_path()
    for deck_id, stored_path, sha in rows:
        ok = False
        if base is not None and stored_path and ".." not in Path(stored_path).parts:
            try:
                ok = hashlib.sha256((base / stored_path).read_bytes()).hexdigest() == sha
            except OSError:
                ok = False
        bind.execute(sa.update(decks).where(decks.c.id == deck_id).values(content_missing=not ok))


def upgrade() -> None:
    have = _have("decks")
    if "storage" not in have:
        op.add_column("decks", sa.Column("storage", sa.String(10), nullable=False, server_default="local"))
    if "content_missing" not in have:
        op.add_column("decks", sa.Column("content_missing", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "channel" not in _have("follow_ups"):
        op.add_column("follow_ups", sa.Column("channel", sa.String(10), nullable=True))
    _backfill()


def downgrade() -> None:
    if "channel" in _have("follow_ups"):
        op.drop_column("follow_ups", "channel")
    have = _have("decks")
    for name in ("content_missing", "storage"):
        if name in have:
            op.drop_column("decks", name)
