"""Phase 2 CRM: companies, contacts, prospects, leads, clients, follow-ups, flags, timeline,
competitors, channel groups, import batches, sheet bookkeeping; FKs on the CRM links of the
Phase 3 tables; sheet columns on enquiries; backfill of accepted enquiries; competitor seeds.

``outreach_messages`` is referenced by ``outreach_events`` / ``outreach_replies`` (CASCADE) and by
itself, and SQLite needs a table rebuild to add a foreign key. Rebuilding with ``foreign_keys=ON``
would cascade-delete the children, so the rebuild runs inside an autocommit block with
``PRAGMA foreign_keys=OFF`` + ``foreign_key_check`` + ``foreign_keys=ON`` (the four-step recipe;
child row counts are asserted unchanged). ``enquiries`` is FK-referenced too, so its new columns
are plain ``ALTER TABLE ... ADD COLUMN`` (no rebuild on upgrade).

The helpers marked FROZEN are copies of ``services/crm/_keys.py``; ``tests/unit/crm/test_keys.py``
asserts they equal the app versions. Never import app code here.

Revision ID: p6_crm_5b2e7d14
Revises: p5_replies_c3d81f20
Create Date: 2026-10-28
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, date, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "p6_crm_5b2e7d14"
down_revision: str | Sequence[str] | None = "p5_replies_c3d81f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ---- frozen enum values at this revision -------------------------------------------------
PROSPECT_KINDS = ("company_enquiry", "outreach_person")
PROSPECT_STAGES = ("new", "contacted", "follow_up_1", "follow_up_2", "final_follow_up")
LEAD_STAGES = ("meeting_scheduled", "proposal_sent", "won", "lost", "no_response")
STAGE_SOURCES = ("app", "sheet")
PROSPECT_STATUSES = ("open", "converted", "closed")
PROSPECT_SOURCES = ("enquiry", "sheet", "manual", "import")
LEAD_OPENED_BY = ("reply", "manual", "sheet")
CLIENT_STATUSES = ("active", "legacy")
CLIENT_SOURCES = ("lead", "legacy_import", "manual", "sheet")
FOLLOW_UP_KINDS = ("follow_up_1", "follow_up_2", "final_follow_up", "custom")
FLAG_ENTITY_TYPES = ("prospect", "enquiry", "contact", "company", "client", "lead")
FLAG_KINDS = ("repeat_poster", "competitor_mention", "no_follow_up")
FLAG_SOURCES = ("auto", "user")
TIMELINE_SUBJECTS = ("prospect", "lead", "client", "contact", "company", "enquiry", "competitor", "group")
TIMELINE_KINDS = (
    "created", "stage_change", "note", "email_sent", "reply_received", "lead_created", "lead_stage",
    "client_created", "proposal_sent", "enquiry_linked", "flag_set", "flag_cleared", "follow_up_added",
    "follow_up_done", "follow_up_cancelled", "tracked_changed", "contact_merged", "contact_linked",
    "call", "meeting", "research", "import", "sheet_created", "sheet_updated", "sheet_removed",
    "sheet_restored", "sheet_status",
)
CONTACT_ORIGINS = ("manual", "enquiry", "sheet", "import", "reply")
TAG_CATEGORIES = ("role", "segment", "custom")
LINK_ENTITY_TYPES = ("enquiry", "prospect", "lead", "client", "competitor", "company")
MENTION_ENTITY_TYPES = ("enquiry", "client", "prospect")
MENTION_SOURCES = ("auto", "user")
KEEP_LEVELS = ("keep", "keep_low")
IMPORT_KINDS = (
    "enquiries", "discarded", "outreach", "prospects", "working", "competitors", "groups", "clients",
    "contacts", "paste",
)
SHEET_ENTITIES = ("enquiry", "discarded", "prospect", "competitor", "group", "metric")
SHEET_TARGET_TYPES = ("enquiry", "prospect", "competitor", "channel_group", "sheet_metric")
METRIC_SETS = ("weekly", "secondary")
SEED_COMPETITORS = ("Future Labs", "Savya Technologies", "White Spaces", "Evolvv", "Ritu Arora")

OUTREACH_FKS = (
    ("prospect_id", "prospects", "fk_outreach_messages_prospect_id_prospects"),
    ("enquiry_id", "enquiries", "fk_outreach_messages_enquiry_id_enquiries"),
    ("company_id", "companies", "fk_outreach_messages_company_id_companies"),
)
ENQUIRY_NEW_COLUMNS = (
    "prospect_id", "company_id", "contact_id", "origin", "sheet_status", "sheet_notes", "sheet_removed_at", "sheet_fields",
)


# ---- frozen helpers (copies of services/crm/_keys.py) ----------------------------------------
_WS = re.compile(r"\s+")
_EMAIL = re.compile(r"[^\s,;<>()\[\]\"']+@[^\s,;<>()\[\]\"']+\.[A-Za-z]{2,}")


def name_key(text: str | None) -> str:
    return _WS.sub(" ", (text or "").strip()).lower()


def phone_digits(phone: str | None) -> str:
    return re.sub(r"\D", "", phone or "")[-10:]


def first_email(text: str | None) -> str | None:
    m = _EMAIL.search(text or "")
    return m.group(0).strip(".").lower() if m else None


def search_text(full_name: str | None, emails: Sequence[str], company: str | None) -> str:
    return " ".join(p for p in [full_name or "", *emails, company or ""] if p).strip().lower()[:700]


# ---- DDL helpers -------------------------------------------------------------------------
def _in(column: str, values: tuple[str, ...], name: str, *, nullable: bool = False) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    expr = f"{column} IN ({allowed})"
    if nullable:
        expr = f"{column} IS NULL OR {expr}"
    return sa.CheckConstraint(expr, name=name)


def _ts(name: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _created_updated() -> list[sa.Column]:
    return [_ts("created_at", nullable=False), _ts("updated_at", nullable=False)]


def _fk(target: str, ondelete: str = "SET NULL", **kw: object) -> sa.ForeignKey:
    return sa.ForeignKey(target, ondelete=ondelete)


def _sheet_columns() -> list[sa.Column]:
    return [
        sa.Column("sheet_status", sa.String(200), nullable=True),
        sa.Column("sheet_notes", sa.Text(), nullable=True),
        _ts("sheet_removed_at"),
        sa.Column("sheet_fields", sa.JSON(), nullable=True),
    ]


def _actor_columns() -> list[sa.Column]:
    return [
        sa.Column("actor", sa.String(80), nullable=False, server_default="system"),
        sa.Column("actor_member_id", sa.Integer(), _fk("team_members.id"), nullable=True),
    ]


def _create_tables() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("name_key", sa.String(200), nullable=False, unique=True),
        sa.Column("website", sa.String(300), nullable=True),
        sa.Column("website_key", sa.String(200), nullable=True, index=True),
        sa.Column("linkedin_url", sa.String(500), nullable=True),
        sa.Column("city", sa.String(100), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        *_created_updated(),
    )
    op.create_table(
        "contacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("full_name", sa.String(200), nullable=True, index=True),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column("phone", sa.String(40), nullable=True),
        sa.Column("linkedin_slug", sa.String(200), nullable=True, unique=True),
        sa.Column("linkedin_url", sa.String(500), nullable=True),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("company_id", sa.Integer(), _fk("companies.id"), nullable=True, index=True),
        sa.Column("search_text", sa.String(700), nullable=False, server_default=""),
        sa.Column("merged_into_id", sa.Integer(), _fk("contacts.id"), nullable=True),
        sa.Column("origin", sa.String(10), nullable=False, server_default="manual"),
        *_actor_columns(),
        *_created_updated(),
        _in("origin", CONTACT_ORIGINS, "origin"),
    )
    op.create_table(
        "contact_emails",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("contact_id", sa.Integer(), _fk("contacts.id", "CASCADE"), nullable=False, index=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "contact_phones",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("contact_id", sa.Integer(), _fk("contacts.id", "CASCADE"), nullable=False, index=True),
        sa.Column("phone", sa.String(40), nullable=False),
        sa.Column("digits", sa.String(20), nullable=False, index=True),
        sa.UniqueConstraint("contact_id", "digits", name="uq_contact_phones_contact_id_digits"),
    )
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("name_key", sa.String(40), nullable=False, unique=True),
        sa.Column("category", sa.String(10), nullable=False, server_default="custom"),
        _ts("created_at", nullable=False),
        _in("category", TAG_CATEGORIES, "category"),
    )
    op.create_table(
        "contact_tags",
        sa.Column("contact_id", sa.Integer(), _fk("contacts.id", "CASCADE"), primary_key=True),
        sa.Column("tag_id", sa.Integer(), _fk("tags.id", "CASCADE"), primary_key=True, index=True),
    )
    op.create_table(
        "contact_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("contact_id", sa.Integer(), _fk("contacts.id", "CASCADE"), nullable=False),
        sa.Column("entity_type", sa.String(12), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(40), nullable=True),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint("contact_id", "entity_type", "entity_id", name="uq_contact_links_contact_entity"),
        sa.Index("ix_contact_links_entity", "entity_type", "entity_id"),
        _in("entity_type", LINK_ENTITY_TYPES, "entity_type"),
    )
    op.create_table(
        "prospects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False, server_default="company_enquiry"),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("linkedin_profile_id", sa.Integer(), _fk("linkedin_profiles.id", "RESTRICT"), nullable=True, unique=True),
        sa.Column("company_id", sa.Integer(), _fk("companies.id"), nullable=True, index=True),
        sa.Column("contact_id", sa.Integer(), _fk("contacts.id"), nullable=True, index=True),
        sa.Column("origin_enquiry_id", sa.Integer(), _fk("enquiries.id"), nullable=True),
        sa.Column("stage", sa.String(20), nullable=False, server_default="new"),
        sa.Column("stage_source", sa.String(6), nullable=False, server_default="app"),
        sa.Column("status", sa.String(10), nullable=False, server_default="open"),
        sa.Column("closed_reason", sa.String(60), nullable=True),
        _ts("closed_at"),
        _ts("contacted_at"),
        sa.Column("contacted_estimated", sa.Boolean(), nullable=False, server_default=sa.false()),
        _ts("responded_at"),
        sa.Column("next_follow_up_on", sa.Date(), nullable=True, index=True),
        sa.Column("source", sa.String(10), nullable=False, server_default="enquiry"),
        sa.Column("reason_url", sa.String(1000), nullable=True),
        sa.Column("reason_text", sa.String(500), nullable=True),
        sa.Column("extra_link", sa.String(1000), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        *_actor_columns(),
        *_sheet_columns(),
        *_created_updated(),
        sa.Index("ix_prospects_status_stage", "status", "stage"),
        _in("kind", PROSPECT_KINDS, "kind"),
        _in("stage", PROSPECT_STAGES, "stage"),
        _in("stage_source", STAGE_SOURCES, "stage_source"),
        _in("status", PROSPECT_STATUSES, "status"),
        _in("source", PROSPECT_SOURCES, "source"),
    )
    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("prospect_id", sa.Integer(), _fk("prospects.id", "CASCADE"), nullable=False, index=True),
        sa.Column("company_id", sa.Integer(), _fk("companies.id"), nullable=True),
        sa.Column("stage", sa.String(20), nullable=False, server_default="meeting_scheduled"),
        sa.Column("stage_source", sa.String(6), nullable=False, server_default="app"),
        _ts("opened_at", nullable=False),
        _ts("closed_at"),
        _ts("meeting_at"),
        sa.Column("proposal_sent_on", sa.Date(), nullable=True),
        sa.Column("value_estimate", sa.Integer(), nullable=True),
        sa.Column("outcome_note", sa.String(500), nullable=True),
        sa.Column("opened_by", sa.String(10), nullable=False, server_default="manual"),
        *_actor_columns(),
        *_created_updated(),
        sa.Index("ix_leads_stage_closed_at", "stage", "closed_at"),
        sa.Index(
            "uq_leads_one_open_per_prospect", "prospect_id", unique=True,
            sqlite_where=sa.text("closed_at IS NULL"), postgresql_where=sa.text("closed_at IS NULL"),
        ),
        _in("stage", LEAD_STAGES, "stage"),
        _in("stage_source", STAGE_SOURCES, "stage_source"),
        _in("opened_by", LEAD_OPENED_BY, "opened_by"),
    )
    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("company_id", sa.Integer(), _fk("companies.id"), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="active"),
        sa.Column("source", sa.String(15), nullable=False, server_default="manual"),
        sa.Column("lead_id", sa.Integer(), _fk("leads.id"), nullable=True, unique=True),
        sa.Column("prospect_id", sa.Integer(), _fk("prospects.id"), nullable=True),
        sa.Column("started_on", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        *_actor_columns(),
        *_created_updated(),
        sa.Index("ix_clients_status", "status"),
        _in("status", CLIENT_STATUSES, "status"),
        _in("source", CLIENT_SOURCES, "source"),
    )
    op.create_table(
        "follow_ups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("prospect_id", sa.Integer(), _fk("prospects.id", "CASCADE"), nullable=True, index=True),
        sa.Column("lead_id", sa.Integer(), _fk("leads.id", "CASCADE"), nullable=True, index=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("auto", sa.Boolean(), nullable=False, server_default=sa.false()),
        _ts("done_at"),
        sa.Column("done_actor", sa.String(80), nullable=True),
        _ts("cancelled_at"),
        sa.Column("cancel_reason", sa.String(80), nullable=True),
        *_created_updated(),
        sa.Index("ix_follow_ups_due", "due_on", "done_at", "cancelled_at"),
        _in("kind", FOLLOW_UP_KINDS, "kind"),
        sa.CheckConstraint(
            "(prospect_id IS NOT NULL AND lead_id IS NULL) OR (prospect_id IS NULL AND lead_id IS NOT NULL)",
            name="one_owner",
        ),
    )
    op.create_table(
        "flags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_type", sa.String(10), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("source", sa.String(5), nullable=False, server_default="auto"),
        sa.Column("detail", sa.JSON(), nullable=True),
        _ts("cleared_at"),
        sa.Column("cleared_by", sa.String(80), nullable=True),
        *_created_updated(),
        sa.Index("ix_flags_kind_cleared_at", "kind", "cleared_at"),
        sa.Index(
            "uq_flags_active", "entity_type", "entity_id", "kind", unique=True,
            sqlite_where=sa.text("cleared_at IS NULL"), postgresql_where=sa.text("cleared_at IS NULL"),
        ),
        _in("entity_type", FLAG_ENTITY_TYPES, "entity_type"),
        _in("kind", FLAG_KINDS, "kind"),
        _in("source", FLAG_SOURCES, "source"),
    )
    op.create_table(
        "timeline_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_type", sa.String(12), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("prospect_id", sa.Integer(), _fk("prospects.id"), nullable=True),
        sa.Column("enquiry_id", sa.Integer(), _fk("enquiries.id"), nullable=True),
        sa.Column("company_id", sa.Integer(), _fk("companies.id"), nullable=True),
        sa.Column("contact_id", sa.Integer(), _fk("contacts.id"), nullable=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("actor", sa.String(80), nullable=False, server_default="system"),
        sa.Column("actor_member_id", sa.Integer(), _fk("team_members.id"), nullable=True),
        _ts("occurred_at", nullable=False),
        _ts("created_at", nullable=False),
        sa.Index("ix_timeline_events_prospect_occurred", "prospect_id", "occurred_at"),
        sa.Index("ix_timeline_events_company_occurred", "company_id", "occurred_at"),
        sa.Index("ix_timeline_events_contact_occurred", "contact_id", "occurred_at"),
        sa.Index("ix_timeline_events_enquiry_occurred", "enquiry_id", "occurred_at"),
        sa.Index("ix_timeline_events_subject_occurred", "subject_type", "subject_id", "occurred_at"),
        _in("subject_type", TIMELINE_SUBJECTS, "subject_type"),
        _in("kind", TIMELINE_KINDS, "kind"),
    )
    op.create_table(
        "competitors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("name_key", sa.String(200), nullable=False, unique=True),
        sa.Column("website", sa.String(300), nullable=True),
        sa.Column("website_key", sa.String(200), nullable=True, unique=True),
        sa.Column("approx_clients", sa.Integer(), nullable=True),
        sa.Column("client_names", sa.Text(), nullable=True),
        sa.Column("contact", sa.String(300), nullable=True),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column("emails", sa.JSON(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("company_id", sa.Integer(), _fk("companies.id"), nullable=True),
        *_sheet_columns(),
        *_created_updated(),
    )
    op.create_table(
        "competitor_mentions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("competitor_id", sa.Integer(), _fk("competitors.id", "CASCADE"), nullable=False),
        sa.Column("entity_type", sa.String(10), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(5), nullable=False, server_default="auto"),
        sa.Column("snippet", sa.String(200), nullable=True),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint("competitor_id", "entity_type", "entity_id", name="uq_competitor_mentions_entity"),
        sa.Index("ix_competitor_mentions_entity", "entity_type", "entity_id"),
        _in("entity_type", MENTION_ENTITY_TYPES, "entity_type"),
        _in("source", MENTION_SOURCES, "source"),
    )
    op.create_table(
        "channel_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("name_key", sa.String(200), nullable=False),
        sa.Column("platform", sa.String(20), nullable=False, server_default="whatsapp"),
        sa.Column("link", sa.String(500), nullable=True),
        sa.Column("link_key", sa.String(500), nullable=True, unique=True),
        sa.Column("details", sa.String(200), nullable=True),
        sa.Column("keep_level", sa.String(10), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.Text(), nullable=True),
        *_sheet_columns(),
        *_created_updated(),
        sa.Index("ix_channel_groups_platform_name", "platform", "name_key"),
        _in("keep_level", KEEP_LEVELS, "keep_level", nullable=True),
    )
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("rows_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_new", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_merged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_unchanged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_error", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", sa.JSON(), nullable=True),
        sa.Column("actor", sa.String(80), nullable=False, server_default="import"),
        sa.Column("actor_member_id", sa.Integer(), _fk("team_members.id"), nullable=True),
        _ts("created_at", nullable=False),
        _in("kind", IMPORT_KINDS, "kind"),
    )
    op.create_table(
        "sheet_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity", sa.String(12), nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("target_type", sa.String(14), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("sheet_id", sa.String(64), nullable=False),
        sa.Column("gid", sa.String(20), nullable=False),
        sa.Column("tab_name", sa.String(60), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("row_hash", sa.String(40), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=True),
        _ts("first_seen_at", nullable=False),
        _ts("last_seen_at", nullable=False),
        sa.UniqueConstraint("entity", "key", "sheet_id", "gid", name="uq_sheet_sources_entity_key_sheet_gid"),
        sa.Index("ix_sheet_sources_sheet_gid", "sheet_id", "gid"),
        sa.Index("ix_sheet_sources_target", "target_type", "target_id"),
        _in("entity", SHEET_ENTITIES, "entity"),
        _in("target_type", SHEET_TARGET_TYPES, "target_type"),
    )
    op.create_table(
        "sheet_metrics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("metric_set", sa.String(10), nullable=False),
        sa.Column("group_label", sa.String(200), nullable=True),
        sa.Column("metric", sa.String(200), nullable=False),
        sa.Column("metric_key", sa.String(300), nullable=False, unique=True),
        sa.Column("weekly_target", sa.Float(), nullable=True),
        sa.Column("weekly_target_raw", sa.String(60), nullable=True),
        _ts("sheet_removed_at"),
        sa.Column("sheet_fields", sa.JSON(), nullable=True),
        _ts("updated_at", nullable=False),
        _in("metric_set", METRIC_SETS, "metric_set"),
    )
    op.create_table(
        "sheet_metric_weeks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("metric_id", sa.Integer(), _fk("sheet_metrics.id", "CASCADE"), nullable=False, index=True),
        sa.Column("week_no", sa.Integer(), nullable=False),
        sa.Column("occurrence", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("label", sa.String(20), nullable=False, server_default=""),
        sa.Column("raw", sa.String(60), nullable=False, server_default=""),
        sa.Column("value", sa.Float(), nullable=True),
        sa.UniqueConstraint("metric_id", "week_no", "occurrence", name="uq_sheet_metric_weeks_metric_week_occ"),
    )


# ---- enquiries: plain ADD COLUMN (no rebuild) --------------------------------------------
def _add_enquiry_columns() -> None:
    for col, target in (("prospect_id", "prospects"), ("company_id", "companies"), ("contact_id", "contacts")):
        op.execute(f"ALTER TABLE enquiries ADD COLUMN {col} INTEGER REFERENCES {target}(id) ON DELETE SET NULL")
        op.create_index(f"ix_enquiries_{col}", "enquiries", [col])
    op.execute("ALTER TABLE enquiries ADD COLUMN origin VARCHAR(10) NOT NULL DEFAULT 'discovery'")
    op.add_column("enquiries", sa.Column("sheet_status", sa.String(200), nullable=True))
    op.add_column("enquiries", sa.Column("sheet_notes", sa.Text(), nullable=True))
    op.add_column("enquiries", _ts("sheet_removed_at"))
    op.add_column("enquiries", sa.Column("sheet_fields", sa.JSON(), nullable=True))


# ---- FK rebuilds ---------------------------------------------------------------------------
_CHILDREN = ("outreach_events", "outreach_replies", "outreach_messages")


def _counts(bind: sa.Connection, tables: Sequence[str]) -> dict[str, int]:
    return {t: int(bind.execute(sa.text(f"SELECT COUNT(*) FROM {t}")).scalar_one()) for t in tables}


def _self_fk() -> sa.Column:
    # Named (as p5 named it): p5's downgrade drops this constraint by name.
    return sa.Column(
        "follow_up_of_id", sa.Integer(),
        sa.ForeignKey("outreach_messages.id", ondelete="SET NULL", name="fk_outreach_messages_follow_up_of_id_outreach_messages"),
    )


def _rebuild_outreach_messages(*, add: bool) -> None:
    """Four-step recipe: OFF -> rebuild -> foreign_key_check -> ON (in ``finally``)."""
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        for col, target, name in OUTREACH_FKS:
            if add:
                op.create_foreign_key(name, "outreach_messages", target, [col], ["id"], ondelete="SET NULL")
            else:
                op.drop_constraint(name, "outreach_messages", type_="foreignkey")
        return
    before = _counts(bind, _CHILDREN)
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
        try:
            with op.batch_alter_table("outreach_messages", recreate="always", reflect_args=[_self_fk()]) as batch:
                for col, target, name in OUTREACH_FKS:
                    if add:
                        batch.create_foreign_key(name, target, [col], ["id"], ondelete="SET NULL")
                    else:
                        batch.drop_constraint(name, type_="foreignkey")
            bad = bind.execute(sa.text("PRAGMA foreign_key_check(outreach_messages)")).fetchall()
            if bad:
                raise RuntimeError(f"foreign_key_check found {len(bad)} violation(s) after rebuilding outreach_messages")
        finally:
            op.execute("PRAGMA foreign_keys=ON")
    after = _counts(bind, _CHILDREN)
    if before != after:
        raise RuntimeError(f"outreach_messages rebuild changed row counts: {before} -> {after}")


def _company_brief_fks(*, add: bool) -> None:
    with op.batch_alter_table("company_briefs") as batch:
        for col, target in (("prospect_id", "prospects"), ("enquiry_id", "enquiries")):
            name = f"fk_company_briefs_{col}_{target}"
            if add:
                batch.create_foreign_key(name, target, [col], ["id"], ondelete="SET NULL")
            else:
                batch.drop_constraint(name, type_="foreignkey")


# ---- backfill ------------------------------------------------------------------------------
def _naive_utc_midnight(d: date | None) -> datetime | None:
    return datetime(d.year, d.month, d.day) if d else None


def _backfill(bind: sa.Connection) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    T = sa.table  # noqa: N806
    C = sa.column  # noqa: N806
    enq = T(
        "enquiries", C("id", sa.Integer), C("review_status", sa.String), C("company", sa.String), C("contact_person", sa.String),
        C("email", sa.String), C("phone", sa.String), C("author_profile_id", sa.Integer), C("author_name", sa.String),
        C("date_added", sa.DateTime), C("decided_at", sa.DateTime), C("date_email_sent", sa.Date),
        C("prospect_id", sa.Integer), C("company_id", sa.Integer), C("contact_id", sa.Integer),
    )
    prof = T("linkedin_profiles", C("id", sa.Integer), C("display_name", sa.String), C("tracked", sa.Boolean))
    msgs = T("outreach_messages", C("id", sa.Integer), C("enquiry_id", sa.Integer), C("state", sa.String),
             C("sent_at", sa.DateTime), C("subject", sa.String))
    companies = T("companies", C("id", sa.Integer), C("name", sa.String), C("name_key", sa.String),
                  C("created_at", sa.DateTime), C("updated_at", sa.DateTime))
    contacts = T("contacts", C("id", sa.Integer), C("full_name", sa.String), C("email", sa.String), C("phone", sa.String),
                 C("company_id", sa.Integer), C("search_text", sa.String), C("origin", sa.String), C("actor", sa.String),
                 C("created_at", sa.DateTime), C("updated_at", sa.DateTime))
    cemails = T("contact_emails", C("contact_id", sa.Integer), C("email", sa.String), C("is_primary", sa.Boolean))
    cphones = T("contact_phones", C("contact_id", sa.Integer), C("phone", sa.String), C("digits", sa.String))
    clinks = T("contact_links", C("contact_id", sa.Integer), C("entity_type", sa.String), C("entity_id", sa.Integer),
               C("role", sa.String), C("created_at", sa.DateTime))
    prospects = T("prospects", C("id", sa.Integer), C("kind", sa.String), C("name", sa.String), C("linkedin_profile_id", sa.Integer),
                  C("company_id", sa.Integer), C("contact_id", sa.Integer), C("origin_enquiry_id", sa.Integer), C("stage", sa.String),
                  C("stage_source", sa.String), C("status", sa.String), C("contacted_at", sa.DateTime),
                  C("contacted_estimated", sa.Boolean), C("source", sa.String), C("actor", sa.String),
                  C("created_at", sa.DateTime), C("updated_at", sa.DateTime))
    timeline = T("timeline_events", C("subject_type", sa.String), C("subject_id", sa.Integer), C("prospect_id", sa.Integer),
                 C("enquiry_id", sa.Integer), C("company_id", sa.Integer), C("kind", sa.String), C("body", sa.Text),
                 C("data", sa.JSON), C("actor", sa.String), C("occurred_at", sa.DateTime), C("created_at", sa.DateTime))
    comp = T("competitors", C("name", sa.String), C("name_key", sa.String), C("active", sa.Boolean),
             C("created_at", sa.DateTime), C("updated_at", sa.DateTime))

    # 6 (first, independent of the rest): competitor seeds, idempotent by name_key.
    have = set(bind.execute(sa.select(sa.column("name_key")).select_from(sa.table("competitors"))).scalars())
    for name in SEED_COMPETITORS:
        if name_key(name) not in have:
            bind.execute(sa.insert(comp).values(name=name, name_key=name_key(name), active=True, created_at=now, updated_at=now))

    rows = bind.execute(
        sa.select(enq).where(enq.c.review_status == "accepted").order_by(enq.c.date_added, enq.c.id)
    ).all()
    if not rows:
        return
    display_names = dict(bind.execute(sa.select(prof.c.id, prof.c.display_name)).all())

    # 1 companies
    company_ids: dict[str, int] = {}
    for r in rows:
        key = name_key(r.company)[:200]
        if key and key not in company_ids:
            company_ids[key] = int(bind.execute(sa.insert(companies).values(
                name=_WS.sub(" ", r.company.strip())[:200], name_key=key, created_at=now, updated_at=now).returning(companies.c.id)).scalar_one())

    # 2 contacts
    by_email: dict[str, int] = {}
    by_person: dict[tuple[str, str], int] = {}
    contact_phone_seen: set[tuple[int, str]] = set()
    enquiry_contact: dict[int, int] = {}
    company_names = {v: k for k, v in company_ids.items()}
    for r in rows:
        ckey = name_key(r.company)[:200]
        cid_company = company_ids.get(ckey)
        email = first_email(r.email)
        person = (r.contact_person or "").strip() or None
        contact_id: int | None = None
        if email and email in by_email:
            contact_id = by_email[email]
        elif email or person:
            pkey = (name_key(person), ckey) if person else None
            if not email and pkey and pkey in by_person:
                contact_id = by_person[pkey]
            else:
                phone = (r.phone or "").strip() or None
                contact_id = int(bind.execute(sa.insert(contacts).values(
                    full_name=person[:200] if person else None, email=email, phone=phone[:40] if phone else None,
                    company_id=cid_company,
                    search_text=search_text(person, [email] if email else [], company_names.get(cid_company)),
                    origin="enquiry", actor="backfill", created_at=now, updated_at=now).returning(contacts.c.id)).scalar_one())
                if email:
                    by_email[email] = contact_id
                    bind.execute(sa.insert(cemails).values(contact_id=contact_id, email=email, is_primary=True))
                if pkey:
                    by_person[pkey] = contact_id
        if contact_id is None:
            continue
        enquiry_contact[r.id] = contact_id
        digits = phone_digits(r.phone)
        if len(digits) >= 6 and (contact_id, digits) not in contact_phone_seen:
            contact_phone_seen.add((contact_id, digits))
            bind.execute(sa.insert(cphones).values(contact_id=contact_id, phone=(r.phone or "").strip()[:40], digits=digits))
        bind.execute(sa.insert(clinks).values(contact_id=contact_id, entity_type="enquiry", entity_id=r.id,
                                              role="buyer", created_at=now))

    # sent messages per accepted enquiry
    sent: dict[int, list[tuple[int, datetime, str]]] = {}
    for m in bind.execute(sa.select(msgs.c.id, msgs.c.enquiry_id, msgs.c.sent_at, msgs.c.subject)
                          .where(msgs.c.state == "sent", msgs.c.enquiry_id.is_not(None)).order_by(msgs.c.sent_at, msgs.c.id)):
        sent.setdefault(m.enquiry_id, []).append((m.id, m.sent_at or now, m.subject or ""))

    # 3 prospects: grouped by author profile, else one per enquiry
    groups: dict[tuple[str, int], list[sa.Row]] = {}
    for r in rows:
        groups.setdefault(("p", r.author_profile_id) if r.author_profile_id else ("e", r.id), []).append(r)
    for (_kind, _gid), members in groups.items():
        first = members[0]
        pname = (
            (first.company or "").strip() or (first.author_name or "").strip() or (first.contact_person or "").strip()
            or (display_names.get(first.author_profile_id) or "").strip() or "Unnamed prospect"
        )[:200]
        contact_id = next((enquiry_contact[m.id] for m in members if m.id in enquiry_contact), None)
        company_id = next((company_ids[name_key(m.company)[:200]] for m in members if name_key(m.company)[:200] in company_ids), None)
        message_times = [t for m in members for (_i, t, _s) in sent.get(m.id, [])]
        date_times = [_naive_utc_midnight(m.date_email_sent) for m in members if m.date_email_sent]
        contacted_at, estimated, stage = None, False, "new"
        if message_times:
            contacted_at, stage = min(message_times), "contacted"
        elif date_times:
            contacted_at, estimated, stage = min(date_times), True, "contacted"
        pid = int(bind.execute(sa.insert(prospects).values(
            kind="company_enquiry", name=pname, linkedin_profile_id=first.author_profile_id, company_id=company_id,
            contact_id=contact_id, origin_enquiry_id=first.id, stage=stage, stage_source="app", status="open",
            contacted_at=contacted_at, contacted_estimated=estimated, source="enquiry", actor="backfill",
            created_at=now, updated_at=now).returning(prospects.c.id)).scalar_one())
        for i, m in enumerate(members):
            bind.execute(sa.update(enq).where(enq.c.id == m.id).values(
                prospect_id=pid, company_id=company_ids.get(name_key(m.company)[:200]), contact_id=enquiry_contact.get(m.id)))
            stamp = m.decided_at or m.date_added or now
            if i == 0:
                bind.execute(sa.insert(timeline).values(
                    subject_type="prospect", subject_id=pid, prospect_id=pid, enquiry_id=m.id, company_id=company_id,
                    kind="created", body="Prospect created from an accepted enquiry (migration backfill).",
                    data={"backfill": True}, actor="backfill", occurred_at=stamp, created_at=now))
            else:
                bind.execute(sa.insert(timeline).values(
                    subject_type="prospect", subject_id=pid, prospect_id=pid, enquiry_id=m.id, company_id=company_id,
                    kind="enquiry_linked", body="Another enquiry from the same profile was linked.",
                    data={"enquiry_id": m.id, "backfill": True}, actor="backfill", occurred_at=stamp, created_at=now))
            for (mid, sent_at, subject) in sent.get(m.id, []):
                bind.execute(sa.insert(timeline).values(
                    subject_type="prospect", subject_id=pid, prospect_id=pid, enquiry_id=m.id, company_id=company_id,
                    kind="email_sent", body=subject[:300] or None, data={"message_id": mid, "backfill": True},
                    actor="backfill", occurred_at=sent_at, created_at=now))
        if first.author_profile_id:
            bind.execute(sa.update(prof).where(prof.c.id == first.author_profile_id, prof.c.tracked.is_(None)).values(tracked=True))

    # 5 outreach_messages links re-derived from enquiry_id
    bind.execute(sa.text(
        "UPDATE outreach_messages SET "
        "prospect_id = (SELECT e.prospect_id FROM enquiries e WHERE e.id = outreach_messages.enquiry_id), "
        "company_id = (SELECT e.company_id FROM enquiries e WHERE e.id = outreach_messages.enquiry_id) "
        "WHERE enquiry_id IS NOT NULL"
    ))


def upgrade() -> None:
    bind = op.get_bind()
    _create_tables()
    _add_enquiry_columns()
    # The Phase 3 ids pointed at tables that did not exist: they are not trustworthy references.
    op.execute("UPDATE outreach_messages SET prospect_id = NULL, company_id = NULL")
    op.execute("UPDATE outreach_messages SET enquiry_id = NULL WHERE enquiry_id IS NOT NULL AND enquiry_id NOT IN (SELECT id FROM enquiries)")
    op.execute("UPDATE company_briefs SET prospect_id = NULL")
    op.execute("UPDATE company_briefs SET enquiry_id = NULL WHERE enquiry_id IS NOT NULL AND enquiry_id NOT IN (SELECT id FROM enquiries)")
    _rebuild_outreach_messages(add=True)
    _company_brief_fks(add=True)
    _backfill(bind)


def _drop_enquiry_columns() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        for col in ("prospect_id", "company_id", "contact_id"):
            op.drop_index(f"ix_enquiries_{col}", table_name="enquiries")
        for col in reversed(ENQUIRY_NEW_COLUMNS):
            op.drop_column("enquiries", col)
        return
    # SQLite cannot DROP COLUMN a foreign-key column: rebuild with the recipe (children survive).
    kids = ("enquiry_sources", "enquiry_keyword_hits", "triage_decisions", "seen_posts")
    before = _counts(bind, ("enquiries", *kids))
    restate = [
        sa.Column("author_profile_id", sa.Integer(), sa.ForeignKey("linkedin_profiles.id", ondelete="SET NULL")),
        sa.Column("seen_post_id", sa.Integer(), sa.ForeignKey("seen_posts.id", ondelete="SET NULL")),
    ]
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
        try:
            with op.batch_alter_table("enquiries", recreate="always", reflect_args=restate) as batch:
                for col in ("prospect_id", "company_id", "contact_id"):
                    batch.drop_index(f"ix_enquiries_{col}")
                for col in reversed(ENQUIRY_NEW_COLUMNS):
                    batch.drop_column(col)
            bad = bind.execute(sa.text("PRAGMA foreign_key_check(enquiries)")).fetchall()
            if bad:
                raise RuntimeError(f"foreign_key_check found {len(bad)} violation(s) after rebuilding enquiries")
        finally:
            op.execute("PRAGMA foreign_keys=ON")
    if before != _counts(bind, ("enquiries", *kids)):
        raise RuntimeError("enquiries rebuild changed row counts")


def downgrade() -> None:
    _company_brief_fks(add=False)
    _rebuild_outreach_messages(add=False)
    # llm_calls / seen_posts reference nothing of ours; enquiries' new columns reference our tables.
    _drop_enquiry_columns()
    for table in (
        "sheet_metric_weeks", "sheet_metrics", "sheet_sources", "import_batches", "channel_groups",
        "competitor_mentions", "competitors", "timeline_events", "flags", "follow_ups", "clients", "leads",
        "prospects", "contact_links", "contact_tags", "tags", "contact_phones", "contact_emails", "contacts",
        "companies",
    ):
        op.drop_table(table)
