"""q27 (SMA-17, SMA-33): the new stage vocabulary and the enquiry / outreach path of a company.

SMA-17: Follow-up 1 and Follow-up 2 become one "Follow-up" (prospects, companies, enquiry statuses); the lead stages
Meeting scheduled / Assessment / Proposal sent / Interviews all become "Engaged" (the board is now Qualified / Engaged /
On hold). The prospects.stage and leads.stage CHECKs change with them; new leads start at Qualified.

SMA-33: ``companies.pipeline_path`` = 'enquiry' (came from an accepted enquiry: it stays on the Enquiries board while it
is a prospect) or 'outreach' (we reach out to them: the Prospects board). Backfilled from the accepted enquiries.

The downgrade is lossy by nature: Follow-up goes back to Follow-up 1, Engaged / Qualified to Meeting scheduled.

Revision ID: q27_stages_paths_4c8e1a52
Revises: q26_customer_wording_6f1a3c58
Create Date: 2026-11-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q27_stages_paths_4c8e1a52"
down_revision: str | Sequence[str] | None = "q26_customer_wording_6f1a3c58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROSPECT_OLD = ("new", "contacted", "follow_up_1", "follow_up_2", "final_follow_up")
PROSPECT_NEW = ("new", "contacted", "follow_up", "final_follow_up")
LEAD_OLD = ("meeting_scheduled", "assessment", "proposal_sent", "interviews", "on_hold", "won", "lost", "no_response")
LEAD_NEW = ("qualified", "engaged", "on_hold", "won", "lost", "no_response")
FOLLOW_UPS = ("follow_up_1", "follow_up_2")
ENGAGED = ("meeting_scheduled", "assessment", "proposal_sent", "interviews")


def _in(values: Sequence[str]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _rebuild_sqlite(table: str, check: str, values: Sequence[str], updates: Sequence[str], indexes: Sequence[str],
                    default: str | None) -> None:
    """SQLite cannot ALTER a CHECK: rebuild the table with foreign keys OFF (so dropping the old copy fires no ON DELETE
    action anywhere), apply ``updates`` between dropping the old CHECK and adding the new one, then re-create the partial
    indexes a batch copy may lose and verify nothing was lost."""
    bind = op.get_bind()
    before = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
        try:
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.drop_constraint(op.f(check), type_="check")
            for sql in updates:
                op.execute(sql)
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.create_check_constraint("stage", sa.text(f"stage IN {_in(values)}"))
                if default is not None:
                    batch.alter_column("stage", server_default=default, existing_type=sa.String(20), existing_nullable=False)
            bad = bind.execute(sa.text(f"PRAGMA foreign_key_check({table})")).fetchall()
            if bad:
                raise RuntimeError(f"foreign_key_check found {len(bad)} violation(s) after rebuilding {table}")
            for sql in indexes:
                op.execute(sql)
        finally:
            op.execute("PRAGMA foreign_keys=ON")
    after = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
    if before != after:
        raise RuntimeError(f"{table} rebuild changed the row count: {before} -> {after}")


def _set_stages(table: str, values: Sequence[str], updates: Sequence[str], indexes: Sequence[str], default: str | None = None) -> None:
    check = f"ck_{table}_stage"
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint(op.f(check), table, type_="check")
        for sql in updates:
            op.execute(sql)
        op.create_check_constraint("stage", table, sa.text(f"stage IN {_in(values)}"))
        if default is not None:
            op.alter_column(table, "stage", server_default=default, existing_type=sa.String(20), existing_nullable=False)
        return
    _rebuild_sqlite(table, check, values, updates, indexes, default)


PROSPECT_INDEXES = (
    "DROP INDEX IF EXISTS uq_prospects_origin_enquiry",
    "CREATE UNIQUE INDEX uq_prospects_origin_enquiry ON prospects (origin_enquiry_id) WHERE origin_enquiry_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_prospects_status_stage ON prospects (status, stage)",
)
LEAD_INDEXES = (
    "DROP INDEX IF EXISTS uq_leads_one_open_per_prospect",
    "CREATE UNIQUE INDEX uq_leads_one_open_per_prospect ON leads (prospect_id) WHERE closed_at IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_leads_stage_closed_at ON leads (stage, closed_at)",
)


def upgrade() -> None:
    _set_stages("prospects", PROSPECT_NEW, [f"UPDATE prospects SET stage = 'follow_up' WHERE stage IN {_in(FOLLOW_UPS)}"],
                PROSPECT_INDEXES)
    _set_stages("leads", LEAD_NEW, [f"UPDATE leads SET stage = 'engaged' WHERE stage IN {_in(ENGAGED)}"], LEAD_INDEXES, "qualified")
    for col in ("sub_status", "resume_sub_status"):
        op.execute(f"UPDATE companies SET {col} = 'follow_up' WHERE {col} IN {_in(FOLLOW_UPS)}")
        op.execute(f"UPDATE companies SET {col} = 'engaged' WHERE {col} IN {_in(ENGAGED)}")
    op.execute(f"UPDATE enquiries SET deal_status = 'follow_up' WHERE deal_status IN {_in(FOLLOW_UPS)}")
    op.execute(f"UPDATE enquiries SET deal_status = 'engaged' WHERE deal_status IN {_in(ENGAGED)}")
    # SMA-33: plain ADD COLUMN (never a rebuild of the FK-referenced companies table)
    op.add_column("companies", sa.Column("pipeline_path", sa.String(10)))
    op.execute("UPDATE companies SET pipeline_path = CASE WHEN EXISTS (SELECT 1 FROM enquiries e WHERE e.company_id = companies.id "
               "AND e.review_status = 'accepted') THEN 'enquiry' ELSE 'outreach' END WHERE stage IS NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE companies DROP COLUMN pipeline_path")
    for col in ("sub_status", "resume_sub_status"):
        op.execute(f"UPDATE companies SET {col} = 'follow_up_1' WHERE {col} = 'follow_up'")
        op.execute(f"UPDATE companies SET {col} = 'meeting_scheduled' WHERE {col} IN ('qualified', 'engaged')")
    op.execute("UPDATE enquiries SET deal_status = 'follow_up_1' WHERE deal_status = 'follow_up'")
    op.execute("UPDATE enquiries SET deal_status = 'meeting_scheduled' WHERE deal_status IN ('qualified', 'engaged')")
    _set_stages("leads", LEAD_OLD, ["UPDATE leads SET stage = 'meeting_scheduled' WHERE stage IN ('qualified', 'engaged')"], LEAD_INDEXES,
                "meeting_scheduled")
    _set_stages("prospects", PROSPECT_OLD, ["UPDATE prospects SET stage = 'follow_up_1' WHERE stage = 'follow_up'"], PROSPECT_INDEXES)
