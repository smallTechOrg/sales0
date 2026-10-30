"""Split the shared "Unnamed prospect" company: every enquiry that names no company gets its OWN placeholder
company (``Company TBD #<enquiry id> - <hint>``) that can be renamed or replaced via "Attach a company".

q17 backfilled companies from prospect names, so every prospect called "Unnamed prospect" (sheet rows with no
company, author or contact) landed in ONE company and their outreach ledgers were lumped together. Each enquiry of
such a company moves to a new company of its own, with its prospect (found by ``origin_enquiry_id``) and the stage
fields copied from the shared one. The shared company is deleted when nothing points at it any more, otherwise it is
renamed and taken out of the pipeline. Re-runnable: only companies still named like the placeholder are touched.

Revision ID: q18_split_unnamed_c1a47e3b
Revises: q17_company_model_c5e9a2d7
Create Date: 2026-10-30
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "q18_split_unnamed_c1a47e3b"
down_revision: str | Sequence[str] | None = "q17_company_model_c5e9a2d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WS = re.compile(r"\s+")
_COPY = ("stage", "sub_status", "off_ramp", "off_ramp_reason", "off_ramp_at", "resume_stage", "resume_sub_status",
         "stage_source", "stage_changed_at")
_REFERRERS = ("enquiries", "prospects", "leads", "clients", "contacts")


def _hint(row) -> str:  # type: ignore[no-untyped-def]
    hint = (row.author_name or row.contact_person or "").strip()
    if not hint and row.email and "@" in row.email:
        hint = row.email.split("@", 1)[1].strip()
    return hint or (row.requirement_summary or "").strip()


def upgrade() -> None:
    bind = op.get_bind()
    meta = sa.MetaData()
    companies = sa.Table("companies", meta, autoload_with=bind)
    enquiries = sa.Table("enquiries", meta, autoload_with=bind)
    prospects = sa.Table("prospects", meta, autoload_with=bind)
    now = datetime.now(UTC).replace(tzinfo=None)

    shared = list(bind.execute(sa.select(companies).where(
        sa.or_(companies.c.name == "Unnamed prospect", companies.c.name.like("Unnamed company (enquiry %")))))
    for old in shared:
        rows = list(bind.execute(sa.select(
            enquiries.c.id, enquiries.c.author_name, enquiries.c.contact_person, enquiries.c.email,
            enquiries.c.requirement_summary).where(enquiries.c.company_id == old.id).order_by(enquiries.c.id)))
        # one enquiry may keep the old company (it is not shared); only split when there are several
        for e in rows[1:] if len(rows) > 1 else []:
            hint = _hint(e)
            name = f"Company TBD #{e.id}" + (f" - {_WS.sub(' ', hint)[:60]}" if hint else "")
            key = name.lower()
            cid = bind.execute(sa.select(companies.c.id).where(companies.c.name_key == key)).scalar()
            if cid is None:
                values = {c: getattr(old, c) for c in _COPY}
                cid = bind.execute(sa.insert(companies).values(
                    name=name, name_key=key, created_at=now, updated_at=now, **values)).inserted_primary_key[0]
            bind.execute(sa.update(enquiries).where(enquiries.c.id == e.id).values(company_id=cid))
            bind.execute(sa.update(prospects).where(prospects.c.origin_enquiry_id == e.id).values(company_id=cid, name=name[:200]))
        if rows:
            first = rows[0]
            hint = _hint(first)
            name = f"Company TBD #{first.id}" + (f" - {_WS.sub(' ', hint)[:60]}" if hint else "")
            if name.lower() != old.name_key:
                clash = bind.execute(sa.select(companies.c.id).where(companies.c.name_key == name.lower())).scalar()
                if clash is None:
                    bind.execute(sa.update(companies).where(companies.c.id == old.id).values(name=name, name_key=name.lower(), updated_at=now))
                    bind.execute(sa.update(prospects).where(prospects.c.origin_enquiry_id == first.id).values(name=name[:200]))
        else:
            used = any(
                bind.execute(sa.text(f"SELECT 1 FROM {t} WHERE company_id = :c LIMIT 1"), {"c": old.id}).first()
                for t in _REFERRERS if t in sa.inspect(bind).get_table_names()
            )
            if not used:
                bind.execute(sa.delete(companies).where(companies.c.id == old.id))
            else:
                bind.execute(sa.update(companies).where(companies.c.id == old.id).values(
                    stage=None, sub_status=None, name="Unnamed (retired)", name_key="unnamed (retired)"))


def downgrade() -> None:
    """Data-only split: nothing to undo (the new companies stay, which is data-safe)."""
