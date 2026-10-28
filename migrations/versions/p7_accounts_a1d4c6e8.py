"""Phase 5 per-user accounts: team members ARE the users.

* ``team_members`` gains ``password_hash`` (argon2id), ``password_set_at``, ``must_reset``,
  ``last_login_at``; new tables ``member_sessions`` (server-side login sessions, only a hash of the
  cookie id is stored) and ``password_tokens`` (one-time set-password links, only a hash stored).
* The Phase 1 ``users`` table is retired. The five ``actor_user_id`` foreign keys (enquiries,
  keywords, scrape_runs, status_history, triage_decisions) are repointed at ``team_members.id``.
  Any legacy ``users`` rows become INACTIVE team members (email ``legacy-user-<id>@users.invalid`` when
  they had none) with ids above every existing id, and the ``actor_user_id`` values are remapped to
  them, so no attribution is lost. Dangling ids are set to NULL (they could not be valid anyway).
* The FK rebuilds follow p6's four-step recipe (``foreign_keys=OFF`` -> batch rebuild ->
  ``foreign_key_check`` -> ``foreign_keys=ON``); the children of ``enquiries`` survive (counts asserted
  and the foreign-key lists before/after are compared apart from the repointed column).

BACK UP BEFORE UPGRADING (SQLite cannot roll back DDL; this revision is not atomic). ``upgrade()`` is
retry-safe: existing columns/tables are skipped, legacy members are looked up by email before being
created, the remap only touches old user ids (new ids are always larger, so re-running is a no-op), and
a table whose FK already points at ``team_members`` is not rebuilt again. Never import app code here.

Revision ID: p7_accounts_a1d4c6e8
Revises: p7_metrics_9a4f61c3
Create Date: 2026-10-28
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p7_accounts_a1d4c6e8"
down_revision: str | Sequence[str] | None = "p7_metrics_9a4f61c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTOR_TABLES = ("enquiries", "keywords", "scrape_runs", "status_history", "triage_decisions")
NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
NEW_MEMBER_COLUMNS = (
    ("password_hash", sa.Column("password_hash", sa.String(255), nullable=True)),
    ("password_set_at", sa.Column("password_set_at", sa.DateTime(timezone=True), nullable=True)),
    ("must_reset", sa.Column("must_reset", sa.Boolean(), nullable=False, server_default=sa.false())),
    ("last_login_at", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True)),
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _drop_batch_leftovers() -> None:
    for name in _tables():
        if name.startswith("_alembic_tmp_"):
            op.execute(f'DROP TABLE "{name}"')


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _create_tables() -> None:
    have = _tables()
    if "member_sessions" not in have:
        op.create_table(
            "member_sessions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("member_id", sa.Integer(), sa.ForeignKey("team_members.id", ondelete="CASCADE"), nullable=False),
            sa.Column("sid_hash", sa.String(64), nullable=False),
            sa.Column("prev_sid_hash", sa.String(64), nullable=True),  # previous cookie id, valid for a short grace
            sa.Column("prev_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("remember", sa.Boolean(), nullable=False, server_default=sa.false()),
            _ts("created_at"),
            _ts("rotated_at"),
            _ts("expires_at"),
            sa.Column("last_ip", sa.String(64), nullable=True),
            sa.UniqueConstraint("sid_hash", name="uq_member_sessions_sid_hash"),
        )
        op.create_index("ix_member_sessions_member_id", "member_sessions", ["member_id"])
    if "password_tokens" not in have:
        op.create_table(
            "password_tokens",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("member_id", sa.Integer(), sa.ForeignKey("team_members.id", ondelete="CASCADE"), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            _ts("created_at"),
            _ts("expires_at"),
            _ts("used_at", nullable=True),
            sa.Column("created_by_member_id", sa.Integer(), sa.ForeignKey("team_members.id", ondelete="SET NULL"), nullable=True),
            sa.UniqueConstraint("token_hash", name="uq_password_tokens_token_hash"),
        )
        op.create_index("ix_password_tokens_member_id", "password_tokens", ["member_id"])


def _add_member_columns() -> None:
    have = _columns("team_members")
    for name, column in NEW_MEMBER_COLUMNS:
        if name not in have:
            op.add_column("team_members", column)


# ---------------------------------------------------------------------------- users -> members
def _legacy_members_and_remap(only: Sequence[str] | None = None) -> None:
    """Create inactive team members for legacy ``users`` rows and remap ``actor_user_id`` to them.

    ``only`` (PostgreSQL path) names the tables to remap; their old FK was dropped beforehand, so the
    "still points at users" guard cannot be used."""
    bind = op.get_bind()
    if "users" not in _tables():
        return
    users = bind.execute(sa.text("SELECT id, display_name, email, created_at FROM users ORDER BY id")).fetchall()
    if not users:
        return
    top = max(
        int(bind.execute(sa.text("SELECT COALESCE(MAX(id), 0) FROM team_members")).scalar_one()),
        max(int(u.id) for u in users),
    )
    mapping: dict[int, int] = {}
    for u in users:
        email = (u.email or f"legacy-user-{u.id}@users.invalid").strip().lower()
        row = bind.execute(sa.text("SELECT id FROM team_members WHERE lower(email) = :e"), {"e": email}).fetchone()
        if row is None:
            top += 1
            bind.execute(
                sa.text(
                    "INSERT INTO team_members (id, name, email, role, active, created_at, must_reset) "
                    "VALUES (:id, :name, :email, 'member', 0, :created, 0)"
                ),
                {"id": top, "name": (u.display_name or "Legacy user")[:80], "email": email, "created": u.created_at},
            )
            mapping[int(u.id)] = top
        else:
            mapping[int(u.id)] = int(row.id)
    if all(old == new for old, new in mapping.items()):
        return
    # Remap via a temporary negative range so an old id equal to another user's new id cannot chain.
    for table in ACTOR_TABLES:
        if only is not None:
            if table not in only:
                continue
        elif "users" not in _fk_targets(table, "actor_user_id"):
            continue  # already repointed on an earlier attempt
        for old, new in mapping.items():
            bind.execute(sa.text(f"UPDATE {table} SET actor_user_id = :n WHERE actor_user_id = :o"), {"n": -new, "o": old})
        bind.execute(sa.text(f"UPDATE {table} SET actor_user_id = -actor_user_id WHERE actor_user_id < 0"))


def _fk_targets(table: str, column: str) -> set[str]:
    return {
        fk["referred_table"]
        for fk in sa.inspect(op.get_bind()).get_foreign_keys(table)
        if column in fk["constrained_columns"]
    }


def _fk_signature(table: str) -> list[tuple]:
    return sorted(
        (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
        for fk in sa.inspect(op.get_bind()).get_foreign_keys(table)
        if fk["constrained_columns"] != ["actor_user_id"]
    )


def _restate_inline_ondelete(table: str) -> list[sa.Column]:
    """SQLite reflection drops ``ON DELETE`` of inline column FKs (those added by ``ALTER TABLE ADD COLUMN``);
    p6 added such columns. Read the rule from the stored SQL and restate the column so the rebuild keeps it."""
    bind = op.get_bind()
    sql = bind.execute(sa.text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"), {"n": table}).scalar_one()
    restated: list[sa.Column] = []
    for fk in sa.inspect(bind).get_foreign_keys(table):
        col = fk["constrained_columns"][0]
        if col == "actor_user_id" or fk.get("options", {}).get("ondelete"):
            continue
        m = re.search(
            rf'"?{re.escape(col)}"?\s+[A-Za-z]+[^,]*?REFERENCES\s+"?{re.escape(fk["referred_table"])}"?\s*\(\s*"?id"?\s*\)\s+ON DELETE (SET NULL|CASCADE|RESTRICT|SET DEFAULT)',
            sql,
            re.IGNORECASE,
        )
        if m:
            restated.append(sa.Column(col, sa.Integer(), sa.ForeignKey(f"{fk['referred_table']}.id", ondelete=m.group(1).upper())))
    return restated


def _counts(tables: Sequence[str]) -> dict[str, int]:
    bind = op.get_bind()
    return {t: int(bind.execute(sa.text(f"SELECT COUNT(*) FROM {t}")).scalar_one()) for t in tables}


def _children_of(table: str) -> list[str]:
    insp = sa.inspect(op.get_bind())
    return [t for t in insp.get_table_names() if any(fk["referred_table"] == table for fk in insp.get_foreign_keys(t))]


def _repoint(table: str, *, to: str, frm: str) -> None:
    """Move ``table.actor_user_id`` FK from ``frm`` to ``to`` (rebuild on SQLite)."""
    bind = op.get_bind()
    if to in _fk_targets(table, "actor_user_id") and frm not in _fk_targets(table, "actor_user_id"):
        return
    old_name = f"fk_{table}_actor_user_id_{frm}"
    new_name = f"fk_{table}_actor_user_id_{to}"
    if bind.dialect.name != "sqlite":
        op.drop_constraint(old_name, table, type_="foreignkey")
        op.create_foreign_key(new_name, table, to, ["actor_user_id"], ["id"], ondelete="SET NULL")
        return
    kids = [table, *_children_of(table)]
    before_counts, before_fks = _counts(kids), _fk_signature(table)
    restate = _restate_inline_ondelete(table)
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
        try:
            with op.batch_alter_table(
                table, recreate="always", naming_convention=NAMING, reflect_args=restate
            ) as batch:
                batch.drop_constraint(old_name, type_="foreignkey")
                batch.create_foreign_key(new_name, to, ["actor_user_id"], ["id"], ondelete="SET NULL")
            bad = bind.execute(sa.text(f"PRAGMA foreign_key_check({table})")).fetchall()
            if bad:
                raise RuntimeError(f"foreign_key_check found {len(bad)} violation(s) after rebuilding {table}")
        finally:
            op.execute("PRAGMA foreign_keys=ON")
    if before_counts != _counts(kids):
        raise RuntimeError(f"{table} rebuild changed row counts")
    if before_fks != _fk_signature(table):
        raise RuntimeError(f"{table} rebuild changed its other foreign keys")


def _null_dangling(parent: str) -> None:
    bind = op.get_bind()
    for table in ACTOR_TABLES:
        bind.execute(
            sa.text(f"UPDATE {table} SET actor_user_id = NULL WHERE actor_user_id IS NOT NULL AND actor_user_id NOT IN (SELECT id FROM {parent})")
        )


def _upgrade_postgres() -> None:
    """PostgreSQL has transactional DDL and ALTER ... DROP CONSTRAINT: drop the old FKs, remap, add the new FKs."""
    bind = op.get_bind()
    pending = [t for t in ACTOR_TABLES if "users" in _fk_targets(t, "actor_user_id")]
    for table in pending:
        for fk in sa.inspect(bind).get_foreign_keys(table):
            if fk["referred_table"] == "users" and fk["constrained_columns"] == ["actor_user_id"]:
                op.drop_constraint(fk["name"], table, type_="foreignkey")
    _legacy_members_and_remap(only=pending)
    _null_dangling("team_members")
    for table in pending:
        op.create_foreign_key(
            f"fk_{table}_actor_user_id_team_members", table, "team_members", ["actor_user_id"], ["id"], ondelete="SET NULL"
        )
    # legacy members were inserted with explicit ids: move the serial past them
    bind.execute(sa.text("SELECT setval(pg_get_serial_sequence('team_members', 'id'), COALESCE((SELECT MAX(id) FROM team_members), 0) + 1, false)"))
    if "users" in _tables():
        op.drop_table("users")


def upgrade() -> None:
    _drop_batch_leftovers()
    _add_member_columns()
    _create_tables()
    if op.get_bind().dialect.name != "sqlite":
        _upgrade_postgres()
        return
    if "users" in _tables():
        with op.get_context().autocommit_block():
            op.execute("PRAGMA foreign_keys=OFF")  # the remap writes ids the old FK (-> users) would refuse
            try:
                _legacy_members_and_remap()
                _null_dangling("team_members")
            finally:
                op.execute("PRAGMA foreign_keys=ON")
    for table in ACTOR_TABLES:
        if "users" in _fk_targets(table, "actor_user_id"):
            _repoint(table, to="team_members", frm="users")
    if "users" in _tables():
        op.drop_table("users")


def downgrade() -> None:
    _drop_batch_leftovers()
    if "users" not in _tables():
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("display_name", sa.String(80), nullable=False),
            sa.Column("email", sa.String(254), nullable=True, unique=True),
            _ts("created_at"),
        )
    for table in ACTOR_TABLES:
        op.get_bind().execute(sa.text(f"UPDATE {table} SET actor_user_id = NULL"))  # ids no longer match any user
    for table in ACTOR_TABLES:
        if "team_members" in _fk_targets(table, "actor_user_id"):
            _repoint(table, to="users", frm="team_members")
    for table in ("password_tokens", "member_sessions"):
        if table in _tables():
            op.drop_table(table)
    for name, _column in reversed(NEW_MEMBER_COLUMNS):
        if name in _columns("team_members"):
            # plain ALTER ... DROP COLUMN (SQLite >= 3.35): a batch rebuild would DROP team_members, which
            # a dozen tables reference. These columns have no index, key or foreign key, so it is allowed.
            op.execute(f"ALTER TABLE team_members DROP COLUMN {name}")
