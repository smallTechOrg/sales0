"""Inbox feedback: editable triage criteria, pipeline repeat check, proper post links.

1. ``triage_criteria`` (team-editable include / exclude rules rendered into the triage system prompt) and
   ``triage_criteria_sets`` (one immutable snapshot of the active rules per change). SEEDED with the 13 rules that
   were hard-coded in ``prompts/triage_system.md`` up to triage-v3 (FROZEN copy below; a unit test keeps it equal to
   ``sales0.agent.criteria``), plus snapshot version 1. ``llm_calls.criteria_version`` records which snapshot a
   triage call used.
2. ``enquiries``: ``pipeline_prospect_id`` / ``pipeline_company_id`` / ``pipeline_match`` / ``pipeline_run_id`` (the
   existing pipeline entity a new enquiry matched) and ``discard_source`` ('auto' = system discard, not the team);
   ``scrape_runs.posts_repeat_skipped``. Plain ids (no FK) so no SQLite rebuild of the FK-referenced ``enquiries``.
3. Data fix (post links): an enquiry / enquiry source whose link is a LinkedIn SEARCH page gets the author's profile
   URL instead when one is known (enquiry author URL, its linkedin_profiles row, or the seen post's raw card); an
   enquiry source prefers the enquiry's own post link. Search links with no known author stay (nothing better).

Plain CREATE TABLE / ADD COLUMN, retry-safe (only what is missing). Downgrade drops the new tables / columns; the
link fix is not reverted (the old search links carried no information a profile link lacks).

Revision ID: q14_criteria_5b8e1f3a
Revises: q13_followch_3e7a6c58
Create Date: 2026-10-29
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import sqlalchemy as sa
from alembic import op

revision: str = "q14_criteria_5b8e1f3a"
down_revision: str | Sequence[str] | None = "q13_followch_3e7a6c58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ---------------------------------------------------------------------------- FROZEN seed (= sales0.agent.criteria)
SEED_INCLUDE: tuple[str, ...] = (
    'A company, institute, HR / L&D, CTO / engineering or business person (or someone writing for one) is SEEKING a '
    'training / workshop / consulting / build PARTNER or an AI product: "looking for a training partner / vendor / '
    'company", "need an AI workshop for our team / employees", "looking for an AI trainer for my company / team", '
    '"want an assistant built". -> `opportunity_type=direct_buyer`.',
    "An agency, edtech or training company, recruiter or staffing firm posts a CONCRETE requirement (a named client or "
    "programme, topic, dates, city, batch size or duration) and invites trainers, SMEs, training providers, academies, "
    "organisations or partners to respond with a profile, TOC / curriculum, proposal or commercials. Even if it says "
    '"Trainer requirement", "URGENT", "for our client" or "share your profile and rates", this is a business '
    "opportunity the team routinely pursues. -> `opportunity_type=agency_requirement`. Priority `medium`; `low` when "
    "thin (no dates, no location, vague topic, no way to respond).",
    'An RFP, tender, vendor-empanelment call or "training providers register here" / provider-pool form from a company '
    "or institute. -> `agency_requirement` (or `direct_buyer` when the company is itself the buyer).",
    "A concrete requirement for one or a few freelance / contract trainers to deliver a corporate or institutional "
    "programme (named topic, audience, dates or location), where the poster is NOT clearly only after a cheap hourly "
    "tutor. -> `opportunity_type=individual_trainer_wanted`, priority `low` (`medium` if a company is named and the "
    "audience is a team).",
    "Posts in Hindi, Hinglish or heavy with emoji are judged the same way.",
)
SEED_EXCLUDE: tuple[str, ...] = (
    'A full-time / permanent / on-payroll EMPLOYEE job post (CTC, LPA, salary, "apply with your resume", "immediate '
    'joiners"), including "AI Trainer" or "Corporate Trainer" roles on payroll. (A part-time / contract / freelance '
    'engagement to deliver training or upskill a team is NOT this case, even when worded "We\'re hiring an AI '
    'trainer": that is `genuine`, `individual_trainer_wanted`.)',
    'An individual shopping for ONE cheap tutor: hourly or per-hour rates, tiny personal batch, "weekend batch, DM '
    'your rates", no organisation behind it. -> `individual_trainer_wanted`.',
    'A trainer, consultant or training company ADVERTISES itself, is "open to work", offers batches / services, or '
    "promotes a partner programme. -> `poster_type=job_seeker_or_self_promo`.",
    'Event, webinar, bootcamp, cohort or course promotion (including "register now", "limited seats", discount '
    "codes, counselling calls).",
    "Thought leadership, news, opinion, celebration, a listicle, a product launch or a company hiring announcement for "
    "non-training roles.",
    "A recruiter or agency growing its trainer NETWORK with no concrete requirement at all and no programme, client, "
    'topic list or schedule ("onboarding freelance trainers", "register on our portal for future assignments", payout '
    "per session, no client, topic dates or city).",
    "The request is clearly outside smallTech's offerings (e.g. SAP ABAP, Tally, non-AI, non-technology topics only).",
    "Spam, pyramid / MLM schemes, or irrelevant content.",
)

ENQUIRY_COLUMNS = (
    ("pipeline_prospect_id", lambda: sa.Column("pipeline_prospect_id", sa.Integer(), nullable=True)),
    ("pipeline_company_id", lambda: sa.Column("pipeline_company_id", sa.Integer(), nullable=True)),
    ("pipeline_match", lambda: sa.Column("pipeline_match", sa.String(20), nullable=True)),
    ("pipeline_run_id", lambda: sa.Column("pipeline_run_id", sa.Integer(), nullable=True)),
    ("discard_source", lambda: sa.Column("discard_source", sa.String(10), nullable=True)),
)
FIXTURE_PREFIX = "/__fixtures/linkedin"


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _stamp() -> datetime:
    now = datetime.now(UTC)
    return now.replace(tzinfo=None) if op.get_bind().dialect.name == "sqlite" else now


# ---------------------------------------------------------------------------- FROZEN link helpers (= scraper.urls)
def _path(url: str | None) -> str | None:
    if not url or not url.strip():
        return None
    try:
        path = urlsplit(url.strip()).path or "/"
    except ValueError:
        return None
    return path[len(FIXTURE_PREFIX):] or "/" if path.startswith(FIXTURE_PREFIX) else path


def _kind(url: str | None) -> str | None:
    path = _path(url)
    if path is None:
        return None
    if "/feed/update/" in path or path.startswith("/posts/"):
        return "post"
    if path.startswith(("/in/", "/company/")):
        return "profile"
    if path.startswith("/search/"):
        return "search"
    return "other"


def _http(url: object) -> str | None:
    if not isinstance(url, str) or not url.strip().lower().startswith(("http://", "https://")):
        return None
    url = url.strip()
    if _kind(url) != "profile":
        return None
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))[:1000]


# ---------------------------------------------------------------------------- steps
def _create_tables() -> None:
    have = _tables()
    if "triage_criteria" not in have:
        op.create_table(
            "triage_criteria",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kind", sa.String(10), nullable=False),
            sa.Column("text", sa.String(600), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("source", sa.String(10), nullable=False, server_default="team"),
            sa.Column("from_enquiry_id", sa.Integer(), nullable=True),
            sa.Column("created_by_member_id", sa.Integer(), nullable=True),
            sa.Column("updated_by_member_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["created_by_member_id"], ["team_members.id"], ondelete="SET NULL",
                name="fk_triage_criteria_created_by_member_id_team_members",
            ),
            sa.ForeignKeyConstraint(
                ["updated_by_member_id"], ["team_members.id"], ondelete="SET NULL",
                name="fk_triage_criteria_updated_by_member_id_team_members",
            ),
            sa.CheckConstraint("kind IN ('include', 'exclude')", name=op.f("ck_triage_criteria_kind")),
            sa.CheckConstraint("source IN ('seed', 'team', 'discard')", name=op.f("ck_triage_criteria_source")),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_triage_criteria")),
        )
    if "triage_criteria_sets" not in have:
        op.create_table(
            "triage_criteria_sets",
            sa.Column("version", sa.Integer(), autoincrement=False, nullable=False),
            sa.Column("rules", sa.JSON(), nullable=False),
            sa.Column("note", sa.String(200), nullable=True),
            sa.Column("actor_member_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["actor_member_id"], ["team_members.id"], ondelete="SET NULL",
                name="fk_triage_criteria_sets_actor_member_id_team_members",
            ),
            sa.PrimaryKeyConstraint("version", name=op.f("pk_triage_criteria_sets")),
        )


def _seed() -> None:
    bind = op.get_bind()
    criteria = sa.table(
        "triage_criteria",
        sa.column("kind", sa.String), sa.column("text", sa.String), sa.column("position", sa.Integer),
        sa.column("active", sa.Boolean), sa.column("source", sa.String),
        sa.column("created_at", sa.DateTime), sa.column("updated_at", sa.DateTime),
    )
    sets = sa.table(
        "triage_criteria_sets",
        sa.column("version", sa.Integer), sa.column("rules", sa.JSON), sa.column("note", sa.String),
        sa.column("created_at", sa.DateTime),
    )
    stamp = _stamp()
    if not bind.execute(sa.text("SELECT COUNT(*) FROM triage_criteria")).scalar():
        rows = [("include", t) for t in SEED_INCLUDE] + [("exclude", t) for t in SEED_EXCLUDE]
        bind.execute(
            criteria.insert(),
            [
                {"kind": k, "text": t, "position": (i + 1) * 10, "active": True, "source": "seed",
                 "created_at": stamp, "updated_at": stamp}
                for i, (k, t) in enumerate(rows)
            ],
        )
    if not bind.execute(sa.text("SELECT COUNT(*) FROM triage_criteria_sets")).scalar():
        active = bind.execute(
            sa.text("SELECT kind, text FROM triage_criteria WHERE active = :t ORDER BY kind DESC, position, id"),
            {"t": True},
        ).all()  # kind DESC = 'include' before 'exclude' (prompt order)
        bind.execute(
            sets.insert(),
            [{"version": 1, "rules": [{"kind": k, "text": t} for k, t in active], "note": "Seeded from the built-in rules",
              "created_at": stamp}],
        )


def _add_columns() -> None:
    bind = op.get_bind()
    have = _columns("enquiries")
    for name, make in ENQUIRY_COLUMNS:
        if name not in have:
            op.add_column("enquiries", make())
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("enquiries")}
    if "ix_enquiries_pipeline_run_id" not in indexes:
        op.create_index("ix_enquiries_pipeline_run_id", "enquiries", ["pipeline_run_id"])
    if "posts_repeat_skipped" not in _columns("scrape_runs"):
        op.add_column("scrape_runs", sa.Column("posts_repeat_skipped", sa.Integer(), nullable=False, server_default="0"))
    if "criteria_version" not in _columns("llm_calls"):
        op.add_column("llm_calls", sa.Column("criteria_version", sa.Integer(), nullable=True))


def _fix_search_links() -> None:
    """Search-page links -> the author's profile (enquiries) / the enquiry's post or profile link (sources)."""
    bind = op.get_bind()
    has_raw_card = "raw_card" in _columns("seen_posts")
    rows = bind.execute(
        sa.text(
            "SELECT e.id, e.source_url, e.author_profile_url, lp.profile_url, e.seen_post_id "
            "FROM enquiries e LEFT JOIN linkedin_profiles lp ON lp.id = e.author_profile_id"
        )
    ).all()
    best: dict[int, str | None] = {}  # enquiry id -> its post or profile link after the fix
    for eid, source_url, author_url, profile_url, seen_post_id in rows:
        kind = _kind(source_url)
        if kind in ("post", "profile"):
            best[eid] = source_url
            continue
        fallback = _http(author_url) or _http(profile_url)
        if fallback is None and has_raw_card and seen_post_id is not None:
            card = bind.execute(sa.text("SELECT raw_card FROM seen_posts WHERE id = :i"), {"i": seen_post_id}).scalar()
            if isinstance(card, str):
                try:
                    card = json.loads(card)
                except ValueError:
                    card = None
            if isinstance(card, dict):
                fallback = _http(card.get("author_profile_url"))
        best[eid] = fallback
        if kind == "search" and fallback:
            bind.execute(sa.text("UPDATE enquiries SET source_url = :u WHERE id = :i"), {"u": fallback, "i": eid})
    for sid, eid, url in bind.execute(sa.text("SELECT id, enquiry_id, source_url FROM enquiry_sources")).all():
        if _kind(url) == "search" and best.get(eid):
            bind.execute(sa.text("UPDATE enquiry_sources SET source_url = :u WHERE id = :i"), {"u": best[eid], "i": sid})


def upgrade() -> None:
    _create_tables()
    _seed()
    _add_columns()
    _fix_search_links()


def downgrade() -> None:
    bind = op.get_bind()
    if "criteria_version" in _columns("llm_calls"):
        op.drop_column("llm_calls", "criteria_version")
    if "posts_repeat_skipped" in _columns("scrape_runs"):
        op.drop_column("scrape_runs", "posts_repeat_skipped")
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("enquiries")}
    if "ix_enquiries_pipeline_run_id" in indexes:
        op.drop_index("ix_enquiries_pipeline_run_id", table_name="enquiries")
    have = _columns("enquiries")
    for name, _ in reversed(ENQUIRY_COLUMNS):
        if name in have:
            op.drop_column("enquiries", name)
    tables = _tables()
    for table in ("triage_criteria_sets", "triage_criteria"):
        if table in tables:
            op.drop_table(table)
