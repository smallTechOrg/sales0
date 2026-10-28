"""Widen ``llm_calls.feature`` CHECK with ``'reply'`` (reply classification gets its own feature).

Reply classification used to be booked as ``triage`` which inflated cost-per-enquiry. SQLite cannot alter a
CHECK, so ``llm_calls`` (a leaf: nothing references it) is rebuilt with the four-step recipe
(``PRAGMA foreign_keys=OFF`` -> batch rebuild -> ``foreign_key_check`` -> ``ON``, restored in ``finally``).
Existing reply-classification rows were stored as ``triage`` with ``prompt_version = 'reply-v1'``; the
upgrade re-labels them ``reply`` (they have no enquiry, so cost-per-enquiry was never keyed to them but
the feature totals were inflated). ``downgrade()`` maps ``reply`` back to ``triage`` (spend history is kept)
and restores the narrower CHECK.

Revision ID: p9_reply_feat_c1e7a3b5
Revises: p8_liaccts_b2e5f7a9
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p9_reply_feat_c1e7a3b5"
down_revision: str | Sequence[str] | None = "p8_liaccts_b2e5f7a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURES_OLD = ("ping", "triage", "research", "drafting", "suggest")
FEATURES_NEW = (*FEATURES_OLD, "reply")


def _check_sql(values: tuple[str, ...]) -> str:
    return "feature IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _set_features(values: tuple[str, ...]) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.drop_constraint("ck_llm_calls_feature", "llm_calls", type_="check")
        op.create_check_constraint("ck_llm_calls_feature", "llm_calls", _check_sql(values))
        return
    before = bind.execute(sa.text("SELECT COUNT(*) FROM llm_calls")).scalar_one()
    # seen_post_id was added by ALTER TABLE ADD COLUMN ... REFERENCES; reflection drops its ON DELETE, restate it.
    seen_post_fk = sa.Column("seen_post_id", sa.Integer(), sa.ForeignKey("seen_posts.id", ondelete="SET NULL"))
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
        try:
            with op.batch_alter_table("llm_calls", recreate="always", reflect_args=[seen_post_fk]) as batch:
                batch.drop_constraint(op.f("ck_llm_calls_feature"), type_="check")
                batch.create_check_constraint("feature", sa.text(_check_sql(values)))
            bad = bind.execute(sa.text("PRAGMA foreign_key_check(llm_calls)")).fetchall()
            if bad:
                raise RuntimeError(f"foreign_key_check found {len(bad)} violation(s) after rebuilding llm_calls")
        finally:
            op.execute("PRAGMA foreign_keys=ON")
    after = bind.execute(sa.text("SELECT COUNT(*) FROM llm_calls")).scalar_one()
    if before != after:
        raise RuntimeError(f"llm_calls rebuild changed the row count: {before} -> {after}")


def upgrade() -> None:
    _set_features(FEATURES_NEW)
    op.execute("UPDATE llm_calls SET feature = 'reply' WHERE feature = 'triage' AND prompt_version = 'reply-v1'")


def downgrade() -> None:
    op.execute("UPDATE llm_calls SET feature = 'triage' WHERE feature = 'reply'")
    _set_features(FEATURES_OLD)
