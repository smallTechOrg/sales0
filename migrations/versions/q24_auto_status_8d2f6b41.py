"""q24: lead stages Assessment / Interviews / On hold (leads.stage CHECK) and the manual status override on companies.

Revision ID: q24_auto_status_8d2f6b41
Revises: q23_engage_5c1d8e33
Create Date: 2026-11-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q24_auto_status_8d2f6b41"
down_revision: str | Sequence[str] | None = "q23_engage_5c1d8e33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STAGES_OLD = ("meeting_scheduled", "proposal_sent", "won", "lost", "no_response")
STAGES_NEW = ("meeting_scheduled", "assessment", "proposal_sent", "interviews", "on_hold", "won", "lost", "no_response")


def _check_sql(values: tuple[str, ...]) -> str:
    return "stage IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _set_lead_stages(values: tuple[str, ...]) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.drop_constraint(op.f("ck_leads_stage"), "leads", type_="check")
        op.create_check_constraint("stage", "leads", sa.text(_check_sql(values)))
        return
    before = bind.execute(sa.text("SELECT COUNT(*) FROM leads")).scalar_one()
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
        try:
            with op.batch_alter_table("leads", recreate="always") as batch:
                batch.drop_constraint(op.f("ck_leads_stage"), type_="check")
                batch.create_check_constraint("stage", sa.text(_check_sql(values)))
            bad = bind.execute(sa.text("PRAGMA foreign_key_check(leads)")).fetchall()
            if bad:
                raise RuntimeError(f"foreign_key_check found {len(bad)} violation(s) after rebuilding leads")
            # the partial unique index: make sure it survived the copy WITH its WHERE clause
            op.execute("DROP INDEX IF EXISTS uq_leads_one_open_per_prospect")
            op.execute("CREATE UNIQUE INDEX uq_leads_one_open_per_prospect ON leads (prospect_id) WHERE closed_at IS NULL")
            names = {i["name"] for i in sa.inspect(bind).get_indexes("leads")}
            if "ix_leads_stage_closed_at" not in names:
                op.execute("CREATE INDEX ix_leads_stage_closed_at ON leads (stage, closed_at)")
        finally:
            op.execute("PRAGMA foreign_keys=ON")
    after = bind.execute(sa.text("SELECT COUNT(*) FROM leads")).scalar_one()
    if before != after:
        raise RuntimeError(f"leads rebuild changed the row count: {before} -> {after}")


def upgrade() -> None:
    _set_lead_stages(STAGES_NEW)
    with op.batch_alter_table("companies") as batch:
        batch.add_column(sa.Column("status_override_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("status_override_by", sa.String(80)))
        batch.add_column(sa.Column("status_override_stage", sa.String(10)))
        batch.add_column(sa.Column("status_override_sub", sa.String(20)))


def downgrade() -> None:
    bind = op.get_bind()
    # the new stages fold back into the nearest old one
    bind.execute(sa.text("UPDATE leads SET stage = 'meeting_scheduled' WHERE stage IN ('assessment', 'on_hold')"))
    bind.execute(sa.text("UPDATE leads SET stage = 'proposal_sent' WHERE stage = 'interviews'"))
    bind.execute(sa.text("UPDATE companies SET sub_status = 'meeting_scheduled' WHERE stage = 'lead' AND sub_status IN ('assessment', 'on_hold')"))
    bind.execute(sa.text("UPDATE companies SET sub_status = 'proposal_sent' WHERE stage = 'lead' AND sub_status = 'interviews'"))
    with op.batch_alter_table("companies") as batch:
        for col in ("status_override_sub", "status_override_stage", "status_override_by", "status_override_at"):
            batch.drop_column(col)
    _set_lead_stages(STAGES_OLD)
