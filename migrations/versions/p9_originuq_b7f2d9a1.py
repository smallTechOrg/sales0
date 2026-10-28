"""One prospect per originating enquiry: merge existing duplicates, then a partial UNIQUE index.

``ensure_for_enquiry`` for a profile-less enquiry had no identity to find its prospect by, so two simultaneous
accepts made two prospects with the same ``origin_enquiry_id``. Before the index can exist the duplicates are
merged (``upgrade()`` first, the index last, so a half-done run is simply re-run):

* the OLDEST prospect (lowest id) of each ``origin_enquiry_id`` is kept; later ones are folded into it;
* every child row is repointed, nothing is deleted: ``enquiries``, ``outreach_messages``, ``company_briefs``,
  ``clients``, ``follow_ups``, ``leads`` (a second OPEN lead is closed as ``lost`` with a note, because
  ``uq_leads_one_open_per_prospect`` allows one), ``timeline_events`` (``prospect_id`` AND
  ``subject_type='prospect'`` rows), and the polymorphic ``contact_links`` / ``competitor_mentions`` /
  ``flags`` / ``sheet_sources`` (entries the keeper already has are dropped, an active duplicate flag is cleared);
* the keeper adopts what it lacks (profile, company, contact, earliest contact/response, soonest follow-up,
  notes), the furthest stage and the strongest status (converted > open > closed);
* a ``note`` timeline event on the keeper records each merge, then the loser row is deleted.

``downgrade()`` only drops the index (the merge cannot be undone). Never imports app code.

Revision ID: p9_originuq_b7f2d9a1
Revises: p9_activity_d4a8c2e6
Create Date: 2026-10-28
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "p9_originuq_b7f2d9a1"
down_revision: str | Sequence[str] | None = "p9_activity_d4a8c2e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "uq_prospects_origin_enquiry"
# FROZEN copies (never import app code here).
STAGES = ("new", "contacted", "follow_up_1", "follow_up_2", "final_follow_up")
STATUS_RANK = {"closed": 0, "open": 1, "converted": 2}
# tables whose ``prospect_id`` column simply repoints
SIMPLE_CHILDREN = ("enquiries", "outreach_messages", "company_briefs", "clients", "follow_ups", "timeline_events")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def _tables(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _merge(bind: sa.Connection, keep: int, lose: int, tables: set[str]) -> None:
    q = lambda sql, **p: bind.execute(sa.text(sql), p)  # noqa: E731
    k = q("SELECT * FROM prospects WHERE id = :i", i=keep).mappings().one()
    lo = q("SELECT * FROM prospects WHERE id = :i", i=lose).mappings().one()
    now = _now()

    # unique linkedin_profile_id: free the loser's value first
    profile = k["linkedin_profile_id"]
    if lo["linkedin_profile_id"] is not None:
        q("UPDATE prospects SET linkedin_profile_id = NULL WHERE id = :i", i=lose)
        profile = profile if profile is not None else lo["linkedin_profile_id"]

    # leads: at most one open lead per prospect
    if "leads" in tables:
        keeper_open = q("SELECT 1 FROM leads WHERE prospect_id = :i AND closed_at IS NULL", i=keep).first()
        if keeper_open is not None:
            q(
                "UPDATE leads SET stage = 'lost', closed_at = :n, "
                "outcome_note = COALESCE(outcome_note, 'Closed: merged duplicate prospect #' || :l) "
                "WHERE prospect_id = :l AND closed_at IS NULL",
                n=now, l=lose,
            )
        q("UPDATE leads SET prospect_id = :k WHERE prospect_id = :l", k=keep, l=lose)
    for table in SIMPLE_CHILDREN:
        if table in tables:
            q(f"UPDATE {table} SET prospect_id = :k WHERE prospect_id = :l", k=keep, l=lose)  # noqa: S608
    if "timeline_events" in tables:
        q("UPDATE timeline_events SET subject_id = :k WHERE subject_type = 'prospect' AND subject_id = :l", k=keep, l=lose)

    # polymorphic references to ('prospect', id)
    if "contact_links" in tables:
        q(
            "DELETE FROM contact_links WHERE entity_type = 'prospect' AND entity_id = :l AND contact_id IN "
            "(SELECT contact_id FROM contact_links WHERE entity_type = 'prospect' AND entity_id = :k)",
            k=keep, l=lose,
        )
        q("UPDATE contact_links SET entity_id = :k WHERE entity_type = 'prospect' AND entity_id = :l", k=keep, l=lose)
    if "competitor_mentions" in tables:
        q(
            "DELETE FROM competitor_mentions WHERE entity_type = 'prospect' AND entity_id = :l AND competitor_id IN "
            "(SELECT competitor_id FROM competitor_mentions WHERE entity_type = 'prospect' AND entity_id = :k)",
            k=keep, l=lose,
        )
        q("UPDATE competitor_mentions SET entity_id = :k WHERE entity_type = 'prospect' AND entity_id = :l", k=keep, l=lose)
    if "flags" in tables:
        q(
            "UPDATE flags SET cleared_at = :n, cleared_by = 'merge' WHERE entity_type = 'prospect' AND entity_id = :l "
            "AND cleared_at IS NULL AND kind IN (SELECT kind FROM flags WHERE entity_type = 'prospect' "
            "AND entity_id = :k AND cleared_at IS NULL)",
            n=now, k=keep, l=lose,
        )
        q("UPDATE flags SET entity_id = :k WHERE entity_type = 'prospect' AND entity_id = :l", k=keep, l=lose)
    if "sheet_sources" in tables:
        q("UPDATE sheet_sources SET target_id = :k WHERE target_type = 'prospect' AND target_id = :l", k=keep, l=lose)

    # keeper adopts what it lacks
    stage = max((k["stage"], lo["stage"]), key=lambda s: STAGES.index(s) if s in STAGES else 0)
    status = max((k["status"], lo["status"]), key=lambda s: STATUS_RANK.get(s, 0))
    contacted_at, contacted_est = k["contacted_at"], k["contacted_estimated"]
    if lo["contacted_at"] is not None and (contacted_at is None or str(lo["contacted_at"]) < str(contacted_at)):
        contacted_at, contacted_est = lo["contacted_at"], lo["contacted_estimated"]
    responded = min((v for v in (k["responded_at"], lo["responded_at"]) if v is not None), key=str, default=None)
    follow = min((v for v in (k["next_follow_up_on"], lo["next_follow_up_on"]) if v is not None), key=str, default=None)
    notes = k["notes"]
    if lo["notes"] and lo["notes"] != notes:
        notes = f"{notes}\n{lo['notes']}" if notes else lo["notes"]
    q(
        "UPDATE prospects SET linkedin_profile_id = :prof, company_id = COALESCE(company_id, :co), "
        "contact_id = COALESCE(contact_id, :ct), stage = :st, status = :ss, "
        "closed_reason = CASE WHEN :ss = 'closed' THEN COALESCE(closed_reason, :cr) ELSE NULL END, "
        "closed_at = CASE WHEN :ss = 'closed' THEN COALESCE(closed_at, :ca) ELSE NULL END, "
        "contacted_at = :ctd, contacted_estimated = :cte, responded_at = :rsp, next_follow_up_on = :nf, "
        "notes = :nt, updated_at = :n WHERE id = :k",
        prof=profile, co=lo["company_id"], ct=lo["contact_id"], st=stage, ss=status, cr=lo["closed_reason"],
        ca=lo["closed_at"], ctd=contacted_at, cte=contacted_est, rsp=responded, nf=follow, nt=notes, n=now, k=keep,
    )
    if "timeline_events" in tables:
        q(
            "INSERT INTO timeline_events (subject_type, subject_id, prospect_id, company_id, kind, body, data, actor, "
            "occurred_at, created_at) VALUES ('prospect', :k, :k, :co, 'note', :b, :d, 'system', :n, :n)",
            k=keep, co=k["company_id"] or lo["company_id"], n=now,
            b=f"Merged duplicate prospect #{lose} (same originating enquiry) into this one.",
            d=json.dumps({"system": True, "merged_prospect_id": lose, "origin_enquiry_id": k["origin_enquiry_id"]}),
        )
    q("DELETE FROM prospects WHERE id = :l", l=lose)


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    dup_origins = [
        r[0]
        for r in bind.execute(
            sa.text(
                "SELECT origin_enquiry_id FROM prospects WHERE origin_enquiry_id IS NOT NULL "
                "GROUP BY origin_enquiry_id HAVING COUNT(*) > 1"
            )
        )
    ]
    for origin in dup_origins:
        ids = [r[0] for r in bind.execute(sa.text("SELECT id FROM prospects WHERE origin_enquiry_id = :o ORDER BY id"), {"o": origin})]
        for lose in ids[1:]:
            _merge(bind, ids[0], lose, tables)
    existing = {i["name"] for i in sa.inspect(bind).get_indexes("prospects")}
    if INDEX not in existing:
        op.create_index(
            INDEX, "prospects", ["origin_enquiry_id"], unique=True,
            sqlite_where=sa.text("origin_enquiry_id IS NOT NULL"),
            postgresql_where=sa.text("origin_enquiry_id IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if INDEX in {i["name"] for i in sa.inspect(bind).get_indexes("prospects")}:
        op.drop_index(INDEX, table_name="prospects")
