"""LinkedIn accounts: ``linkedin_accounts`` table, ``scrape_runs.linkedin_account_id``, backfill.

Moves the LinkedIn session / safety / cool-down / daily-counter state off the ``linkedin_session``
singleton: a DEFAULT account row is created from it (folder backend, the existing Chrome profile,
so the single existing account behaves exactly as before) and every existing ``scrape_runs`` row is
stamped with it. ``linkedin_session`` is kept (legacy, no longer read or written).

``scrape_runs`` is FK-referenced (keywords, llm_calls...) and SQLite needs a rebuild to add a FK, so the
new column is a plain ``ALTER TABLE ... ADD COLUMN ... REFERENCES linkedin_accounts(id) ON DELETE SET NULL``
(the same recipe as p6 for ``enquiries``). ``upgrade()`` is retry-safe: the table, column and index are
created only when missing and the backfill only touches rows that are still unset.

``downgrade()`` drops the index, rebuilds ``scrape_runs`` without the column (four-step recipe, as p6)
and drops the table. Never imports app code (the profile directory names below are
frozen copies of ``scraper.browser.PROFILE_DIRNAMES``).

Revision ID: p8_liaccts_b2e5f7a9
Revises: p7_accounts_a1d4c6e8
Create Date: 2026-10-28
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "p8_liaccts_b2e5f7a9"
down_revision: str | Sequence[str] | None = "p7_accounts_a1d4c6e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACCOUNT_STATUSES = ("not_connected", "connected", "needs_attention", "disabled")
BACKENDS = ("folder", "db")
DEFAULT_LABEL = "Default"

# FROZEN copy of scraper.browser.PROFILE_DIRNAMES.
PROFILE_DIRNAMES = {
    ("live", "chrome"): "linkedin-chrome-profile",
    ("live", "chromium"): "linkedin-profile",
    ("fixture", "chrome"): "linkedin-profile-fixture-chrome",
    ("fixture", "chromium"): "linkedin-profile-fixture-chromium",
}


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _profile_dirname() -> str:
    """Best guess at the profile directory the existing account uses (informational: the runtime
    resolves legacy names by the live mode + resolved channel, exactly as before p8)."""
    mode = "fixture" if os.environ.get("SALES0_LINKEDIN_MODE", "live").strip().lower() == "fixture" else "live"
    channel = "chromium" if os.environ.get("SALES0_LINKEDIN_BROWSER_CHANNEL", "auto").strip().lower() == "chromium" else "chrome"
    return PROFILE_DIRNAMES[(mode, channel)]


def _create_table() -> None:
    op.create_table(
        "linkedin_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("owner_member_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="not_connected"),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("backend", sa.String(10), nullable=False, server_default="folder"),
        sa.Column("profile_dirname", sa.String(120), nullable=True),
        sa.Column("session_state", sa.Text(), nullable=True),
        sa.Column("proxy_url", sa.String(300), nullable=True),
        sa.Column("user_agent", sa.String(300), nullable=True),
        sa.Column("locale", sa.String(20), nullable=True),
        sa.Column("timezone", sa.String(60), nullable=True),
        sa.Column("viewport", sa.String(20), nullable=True),
        _ts("last_checked_at", nullable=True),
        _ts("last_connected_at", nullable=True),
        _ts("last_successful_run_at", nullable=True),
        _ts("last_ok_at", nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("safety_state", sa.String(20), nullable=True),
        _ts("last_stop_at", nullable=True),
        sa.Column("last_stop_reason", sa.String(300), nullable=True),
        _ts("cooldown_until", nullable=True),
        sa.Column("counter_day", sa.String(10), nullable=True),
        sa.Column("runs_today", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("posts_today", sa.Integer(), nullable=False, server_default="0"),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.CheckConstraint(_in("status", ACCOUNT_STATUSES), name=op.f("ck_linkedin_accounts_status")),
        sa.CheckConstraint(_in("backend", BACKENDS), name=op.f("ck_linkedin_accounts_backend")),
    )
    op.create_index("ix_linkedin_accounts_owner_member_id", "linkedin_accounts", ["owner_member_id"])


def _add_run_column() -> None:
    insp = sa.inspect(op.get_bind())
    have = {c["name"] for c in insp.get_columns("scrape_runs")}
    if "linkedin_account_id" not in have:
        op.execute(
            "ALTER TABLE scrape_runs ADD COLUMN linkedin_account_id INTEGER "
            "REFERENCES linkedin_accounts(id) ON DELETE SET NULL"
        )
    if "ix_scrape_runs_linkedin_account_id" not in {i["name"] for i in insp.get_indexes("scrape_runs")}:
        op.create_index("ix_scrape_runs_linkedin_account_id", "scrape_runs", ["linkedin_account_id"])


def _backfill() -> None:
    bind = op.get_bind()
    accounts = sa.table("linkedin_accounts", sa.column("id", sa.Integer))
    account_id = bind.execute(sa.select(accounts.c.id).order_by(accounts.c.id).limit(1)).scalar()
    now = datetime.now(UTC).replace(tzinfo=None)
    if account_id is None:
        old = {}
        insp = sa.inspect(bind)
        if "linkedin_session" in insp.get_table_names():
            row = bind.execute(sa.text(
                "SELECT status, last_checked_at, last_connected_at, last_successful_run_at, last_error "
                "FROM linkedin_session WHERE id = 1"
            )).mappings().first()
            old = dict(row) if row else {}
        status = old.get("status") if old.get("status") in ("not_connected", "connected", "needs_attention") else "not_connected"
        res = bind.execute(
            sa.text(
                "INSERT INTO linkedin_accounts (label, status, disabled, backend, profile_dirname, last_checked_at, "
                "last_connected_at, last_successful_run_at, last_ok_at, last_error, runs_today, posts_today, "
                "created_at, updated_at) VALUES (:label, :status, 0, 'folder', :dirname, :checked, :connected, "
                ":run, :ok, :err, 0, 0, :now, :now)"
            ),
            {
                "label": DEFAULT_LABEL,
                "status": status,
                "dirname": _profile_dirname(),
                "checked": old.get("last_checked_at"),
                "connected": old.get("last_connected_at"),
                "run": old.get("last_successful_run_at"),
                "ok": old.get("last_successful_run_at") or old.get("last_connected_at") if status == "connected" else None,
                "err": old.get("last_error"),
                "now": now,
            },
        )
        account_id = res.lastrowid
        if account_id is None:  # non-SQLite dialects
            account_id = bind.execute(sa.select(accounts.c.id).order_by(accounts.c.id).limit(1)).scalar()
    bind.execute(
        sa.text("UPDATE scrape_runs SET linkedin_account_id = :id WHERE linkedin_account_id IS NULL"),
        {"id": account_id},
    )


def upgrade() -> None:
    bind = op.get_bind()
    if "linkedin_accounts" not in sa.inspect(bind).get_table_names():
        _create_table()
    _add_run_column()
    _backfill()


def _count(bind: sa.Connection, table: str) -> int:
    return int(bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


def downgrade() -> None:
    """Drop the index, the ``scrape_runs`` column and the table.

    ``scrape_runs`` is FK-referenced (``scrape_run_keywords`` CASCADEs from it), so on SQLite the column is
    removed by the p6 four-step recipe: ``foreign_keys=OFF`` -> rebuild -> ``foreign_key_check`` -> ON, with
    child row counts asserted unchanged. (A dangling FK to the dropped table would break any later rebuild.)
    """
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "linkedin_account_id" in {c["name"] for c in insp.get_columns("scrape_runs")}:
        if "ix_scrape_runs_linkedin_account_id" in {i["name"] for i in insp.get_indexes("scrape_runs")}:
            op.drop_index("ix_scrape_runs_linkedin_account_id", table_name="scrape_runs")
        if bind.dialect.name != "sqlite":
            op.drop_column("scrape_runs", "linkedin_account_id")
        else:
            children = [t for t in ("scrape_runs", "scrape_run_keywords", "llm_calls", "status_history") if t in insp.get_table_names()]
            before = {t: _count(bind, t) for t in children}
            with op.get_context().autocommit_block():
                op.execute("PRAGMA foreign_keys=OFF")
                try:
                    with op.batch_alter_table("scrape_runs", recreate="always") as batch:
                        batch.drop_column("linkedin_account_id")
                    bad = bind.execute(sa.text("PRAGMA foreign_key_check(scrape_runs)")).fetchall()
                    if bad:
                        raise RuntimeError(f"foreign_key_check found {len(bad)} violation(s) after rebuilding scrape_runs")
                finally:
                    op.execute("PRAGMA foreign_keys=ON")
            after = {t: _count(bind, t) for t in children}
            if before != after:
                raise RuntimeError(f"scrape_runs rebuild changed row counts: {before} -> {after}")
    if "linkedin_accounts" in sa.inspect(bind).get_table_names():
        op.drop_table("linkedin_accounts")
