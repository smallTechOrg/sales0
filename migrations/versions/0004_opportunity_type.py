"""Phase 1.5 recall-first triage: ``enquiries.ai_opportunity_type`` (direct_buyer |
agency_requirement | individual_trainer_wanted | other), backfilled from ``ai_extraction``.

Revision ID: 0004_opportunity_type
Revises: p3_outreach_a7c41e9d
Create Date: 2026-10-28
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_opportunity_type"
down_revision: str | Sequence[str] | None = "p3_outreach_a7c41e9d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALLOWED = ("direct_buyer", "agency_requirement", "individual_trainer_wanted", "other")


@contextmanager
def _children_safe(ignore: Sequence[str] = ()) -> Iterator[None]:
    """Run table rebuilds without cascading into child rows (p6 four-step recipe).

    SQLite's batch rebuild drops the old table; under ``foreign_keys=ON`` that implicit DELETE cascades to
    (or NULLs) the child rows. ``PRAGMA foreign_keys`` is a no-op inside a transaction, hence the autocommit
    block. Afterwards ``foreign_key_check`` must not report more violations than before and every surviving
    table keeps its row count, so a regression fails loudly instead of silently losing data.
    """
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        yield
        return
    tables = [t for t in sa.inspect(bind).get_table_names() if t not in ignore]
    before = {t: bind.execute(sa.text(f'SELECT COUNT(*) FROM "{t}"')).scalar_one() for t in tables}
    bad_before = len(bind.execute(sa.text("PRAGMA foreign_key_check")).fetchall())
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
        try:
            yield
            bad = bind.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
            if len(bad) > bad_before:
                raise RuntimeError(f"foreign_key_check found {len(bad) - bad_before} new violation(s) after the rebuild")
        finally:
            op.execute("PRAGMA foreign_keys=ON")
    live = set(sa.inspect(bind).get_table_names())
    after = {t: bind.execute(sa.text(f'SELECT COUNT(*) FROM "{t}"')).scalar_one() for t in tables if t in live}
    if after != {t: before[t] for t in after}:
        raise RuntimeError(f"downgrade rebuild changed child row counts: {before} -> {after}")


def upgrade() -> None:
    op.add_column("enquiries", sa.Column("ai_opportunity_type", sa.String(length=40), nullable=True))
    bind = op.get_bind()
    enquiries = sa.table(
        "enquiries", sa.column("id", sa.Integer), sa.column("ai_extraction", sa.JSON), sa.column("ai_opportunity_type", sa.String)
    )
    rows = bind.execute(sa.select(enquiries.c.id, enquiries.c.ai_extraction)).all()
    for row_id, extraction in rows:
        value = extraction.get("opportunity_type") if isinstance(extraction, dict) else None
        if value in _ALLOWED:
            bind.execute(enquiries.update().where(enquiries.c.id == row_id).values(ai_opportunity_type=value))


def downgrade() -> None:
    with _children_safe(), op.batch_alter_table("enquiries") as batch:
        batch.drop_column("ai_opportunity_type")
