"""``sales0 pull-data``: copy the hosted database into a LOCAL SQLite file through ``/api/worker/export/*``.

The reverse of ``push_data`` and read-only against production. The local file is built fresh at the SAME Alembic head as
the server (a different head is refused), tables are filled parent-first in the importer's order, and nothing sensitive
travels: the server never exports its ``data_import.SKIP`` tables (team members and passwords, sessions, Gmail tokens,
LinkedIn account state, ...). References to team members are pointed at ONE local admin created here; the LinkedIn
account/session tables keep whatever the migrations seeded (no cookies, no profile). Foreign keys are not enforced
while loading (the server keeps changing between pages, so a child can arrive before a parent that was added a moment
later); the result is checked afterwards and violations are reported as counts.

Only counts are ever printed - never row contents.
"""

from __future__ import annotations

import os
import secrets
import shlex
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alembic import command
from sqlalchemy import Table, delete, event, func, insert, select, text

from sales0.clock import utcnow
from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, current_revision, head_revision
from sales0.errors import DomainError
from sales0.services import data_import as plan
from sales0.worker.client import WorkerClient

ADMIN_EMAIL = "admin@local.test"
ADMIN_ID = 1
PORT = 8780
BATCH_ROWS = 500


class PullError(RuntimeError):
    """A refusal or failure with a message meant for the terminal."""


@dataclass
class PullResult:
    ok: bool
    db_path: Path
    run_script: Path | None = None
    admin_password: str | None = None
    counts: dict[str, tuple[int, int, int]] = field(default_factory=dict)  # table -> (server, received, local)
    fk_violations: int = 0


def check_head(server_head: str | None, local_head: str | None) -> None:
    if server_head != local_head:
        raise PullError(
            f"schema mismatch: this checkout is at migration {local_head or 'none'} but the server is at {server_head or 'none'}. "
            + (
                "The server is behind this code - pull from a deployed build of this branch, or check out the deployed version."
                if _is_ahead(server_head, local_head)
                else "Update the repo (git pull) so it matches the server."
            )
        )


def _is_ahead(server_head: str | None, local_head: str | None) -> bool:
    """True when the local code knows a revision the server does not have yet ('server is behind')."""
    from alembic.script import ScriptDirectory

    try:
        script = ScriptDirectory.from_config(alembic_config())
        if server_head is None:
            return True
        server_known = {r.revision for r in script.walk_revisions()}
        if server_head not in server_known:
            return False  # the server is newer than this checkout
        ancestors = {r.revision for r in script.iterate_revisions(local_head, server_head)}
        return server_head in ancestors and server_head != local_head
    except Exception:  # noqa: BLE001 - the hint is best effort
        return False


def _remap(col: Any, value: Any, accounts: set[int]) -> Any:
    if value is None:
        return None
    for fk in col.foreign_keys:
        target = fk.column.table.name
        if target == plan.MEMBERS:
            return ADMIN_ID
        if target == plan.ACCOUNTS:
            return value if int(value) in accounts else (min(accounts) if accounts and not col.nullable else None)
    return value


def prepare_rows(table: Table, rows: list[dict[str, Any]], accounts: set[int]) -> list[dict[str, Any]]:
    """Coerce the JSON strings back by column type and re-point member / account references."""
    cols = {c.name: c for c in table.columns}
    out: list[dict[str, Any]] = []
    for row in rows:
        unknown = sorted(set(row) - set(cols))
        if unknown:
            raise PullError(f"{table.name}: the server sent column(s) {unknown} this checkout does not have; update the repo.")
        try:
            out.append({name: _remap(cols[name], plan._coerce_value(cols[name], value), accounts) for name, value in row.items()})
        except DomainError as exc:
            raise PullError(exc.message) from exc
    return out


def _disable_fk(engine: Any) -> None:
    @event.listens_for(engine, "connect")
    def _off(dbapi_connection: Any, _record: Any) -> None:  # runs after build_engine's own pragmas
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=OFF")
        finally:
            cursor.close()


def _sqlite_files(path: Path) -> list[Path]:
    return [path, *(path.with_name(path.name + s) for s in ("-wal", "-shm"))]


def pull(
    client: WorkerClient,
    out_path: Path,
    *,
    tables: list[str] | None = None,
    force: bool = False,
    out: Callable[[str], None] = print,
    write_script: bool = True,
) -> PullResult:
    out_path = out_path.expanduser().resolve()
    if out_path.exists() and not force:
        raise PullError(f"{out_path} already exists; pass --force to replace it (the old file is deleted) or choose another --out.")
    manifest = client.export_manifest()
    check_head(manifest.head, head_revision())
    names = [t.name for t in manifest.tables]
    wanted = names
    if tables:
        unknown = sorted(set(tables) - set(names))
        if unknown:
            raise PullError(f"not exportable: {unknown}. Exportable: {', '.join(names)}")
        wanted = [n for n in names if n in set(tables)]  # keep the server's parent-first order
    by_name = plan.importable_tables()
    server_counts = {t.name: t.rows for t in manifest.tables}
    out(f"Server schema {manifest.head} matches this checkout. Pulling {len(wanted)} table(s), {sum(server_counts[n] for n in wanted)} rows.")
    out("Never pulled: " + ", ".join(manifest.excluded))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_name(out_path.name + ".partial")
    for f in _sqlite_files(partial):
        f.unlink(missing_ok=True)
    url = f"sqlite:///{partial}"
    password = secrets.token_urlsafe(16)
    result = PullResult(ok=False, db_path=out_path, admin_password=password)
    engine = build_engine(url)
    _disable_fk(engine)
    try:
        command.upgrade(alembic_config(url), "head")
        if current_revision(engine) != manifest.head:
            raise PullError("the new local database did not reach the server's schema revision.")
        with engine.begin() as conn:
            accounts = {int(r[0]) for r in conn.execute(text("SELECT id FROM linkedin_accounts"))}
            for name in reversed(wanted):  # drop what the migrations seeded; the server's copy replaces it
                conn.execute(delete(by_name[name]))
            _create_admin(conn, password)
            for name in wanted:
                table = by_name[name]
                received = _pull_table(client, conn, table, server_counts[name], accounts, out)
                local = int(conn.execute(select(func.count()).select_from(table)).scalar_one())
                result.counts[name] = (server_counts[name], received, local)
            result.fk_violations = len(conn.execute(text("PRAGMA foreign_key_check")).fetchall())
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    except BaseException:
        engine.dispose()
        for f in _sqlite_files(partial):
            f.unlink(missing_ok=True)
        raise
    engine.dispose()

    out("Per-table check (server -> received -> local):")
    bad = 0
    for name, (server, received, local) in result.counts.items():
        mark = "ok " if local == received else "BAD"
        note = "" if server == received else f"  (server changed while pulling: {server} at start)"
        bad += local != received
        if server or received or local:
            out(f"  {mark} {name:28} {server:>7} -> {received:>7} -> {local:>7}{note}")
    if result.fk_violations:
        out(f"Warning: {result.fk_violations} dangling reference(s) (rows changed on the server mid-pull); the app tolerates them, re-run to refresh.")
    if bad:
        for f in _sqlite_files(partial):
            f.unlink(missing_ok=True)
        raise PullError(f"{bad} table(s) do not match; nothing was written to {out_path}.")
    for f in _sqlite_files(out_path):
        f.unlink(missing_ok=True)
    os.replace(partial, out_path)
    for suffix in ("-wal", "-shm"):
        partial.with_name(partial.name + suffix).unlink(missing_ok=True)
    result.ok = True
    if write_script:
        result.run_script = write_run_script(out_path)
    return result


def _create_admin(conn: Any, password: str) -> None:
    from sales0.services.team.passwords import hash_password

    members = plan._members_table()
    conn.execute(delete(members))
    now = utcnow()
    conn.execute(insert(members).values(
        id=ADMIN_ID, name="Local admin", email=ADMIN_EMAIL, role="admin", active=True, created_at=now,
        password_hash=hash_password(password), password_set_at=now, must_reset=False,
    ))  # fmt: skip


def _pull_table(client: WorkerClient, conn: Any, table: Table, expected: int, accounts: set[int], out: Callable[[str], None]) -> int:
    received = 0
    after: str | None = None
    if expected:
        out(f"  {table.name}: 0/{expected}")
    while True:
        page = client.export_batch(table.name, after, BATCH_ROWS)
        if page.table != table.name:
            raise PullError(f"asked for {table.name}, the server answered {page.table}.")
        if page.rows:
            conn.execute(insert(table), prepare_rows(table, page.rows, accounts))
            received += len(page.rows)
            out(f"  {table.name}: {received}/{max(expected, received)}" + (f" ({page.trimmed} oversized raw capture(s) dropped)" if page.trimmed else ""))
        if page.next is None:
            return received
        if page.next == after or not page.rows:
            raise PullError(f"{table.name}: the server's cursor did not advance; aborting.")
        after = page.next


def write_run_script(db_path: Path) -> Path:
    """``run.sh`` next to the database: every env the pulled copy needs, scheduler and outbound integrations off."""
    from sales0.config import REPO_ROOT

    data_dir = db_path.parent
    q = shlex.quote
    script = f"""#!/usr/bin/env bash
# Generated by `sales0 pull-data`. Runs the app on the PULLED copy only (http://127.0.0.1:{PORT}).
# Nothing here can reach production, send email, or touch LinkedIn:
#   - SALES0_MODE=local, scheduler / Gmail reply poller / sheet sync are OFF (no background work at all)
#   - SALES0_LINKEDIN_MODE=fixture: "run now" talks to the built-in FAKE LinkedIn on localhost, never linkedin.com
#   - SALES0_GMAIL_DAILY_SEND_CAP=0 and no Gmail connection / Google credentials exist in this data dir
#   - the Anthropic key defaults to a dummy (AI features fail harmlessly); export a real one first to use them
set -euo pipefail
cd {q(str(REPO_ROOT))}
export SALES0_MODE=local
export SALES0_DATABASE_URL={q("sqlite:///" + str(db_path))}
export SALES0_DATA_DIR={q(str(data_dir))}
export SALES0_HOST=127.0.0.1
export SALES0_PORT={PORT}
export SALES0_PUBLIC_BASE_URL=http://127.0.0.1:{PORT}
export SALES0_SCHEDULER_ENABLED=false
export SALES0_REPLY_POLL_MINUTES=0
export SALES0_SHEET_SYNC_MINUTES=0
export SALES0_LINKEDIN_MODE=fixture
export SALES0_GMAIL_DAILY_SEND_CAP=0
export SALES0_GOOGLE_TOKEN_PATH={q(str(data_dir / "google_token.json"))}
export SALES0_GOOGLE_CLIENT_SECRET_PATH={q(str(data_dir / "google_client_secret.json"))}
export SALES0_ANTHROPIC_API_KEY="${{SALES0_ANTHROPIC_API_KEY:-sk-ant-pulled-data-dummy-key}}"
echo "sales0 on pulled data: http://127.0.0.1:{PORT}  (log in as {ADMIN_EMAIL}; Ctrl-C to stop)"
exec uv run python -m sales0
"""
    path = data_dir / "run.sh"
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


__all__ = ["ADMIN_EMAIL", "PullError", "PullResult", "pull", "write_run_script"]
