"""One LinkedIn canonicaliser: re-key stored sheet person keys, and allow the ``source_row_removed`` flag.

1. ``sheet_sources.key`` of person rows (``entity='prospect'``) is ``li:<slug>``. The slug used to drop the
   kind (a company page became ``li:acme`` and then an ``in/acme`` PERSON) and skipped percent-decoding / NFC /
   the host check. The key is recomputed from the stored ``fields.linkedin_url`` with the new canonicaliser
   (a FROZEN copy of ``sales0.linkedin_url`` below; ``tests/integration/db/test_migration_p9_lirekey.py`` asserts
   it equals the app version): a person keeps ``li:<slug>``, any other kind becomes ``li:<kind>/<slug>``, a link
   that is not a LinkedIn profile any more falls back to the ``nc:<hash(name, company)>`` key.
   Only ``sheet_sources.key`` changes - no row is added or deleted, so nothing is lost. If the new key is already
   taken by another row of the same tab (two spellings of one link), that row keeps its identity as
   ``row:<row_hash>`` - exactly what the sync's duplicate handling expects - and no prospect is merged.
2. ``flags.kind`` CHECK gains ``'source_row_removed'`` (an enquiry's sheet row vanished but its prospect has
   human history). ``flags`` is a leaf table; SQLite rebuilds it with the four-step recipe (``foreign_keys=OFF`` ->
   batch rebuild -> ``foreign_key_check`` -> ``ON``), the partial unique index is re-created if the rebuild lost it,
   and the row count is asserted unchanged.

``downgrade()`` maps ``li:<kind>/<slug>`` keys back to ``li:<slug>`` (where that is free), deletes the
``source_row_removed`` flags (derived, re-creatable data the narrower CHECK cannot hold) and restores the CHECK.

Revision ID: p9_lirekey_7c3e9a15
Revises: p9_originuq_b7f2d9a1
Create Date: 2026-10-28
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from urllib.parse import unquote, urlsplit

import sqlalchemy as sa
from alembic import op

revision: str = "p9_lirekey_7c3e9a15"
down_revision: str | Sequence[str] | None = "p9_originuq_b7f2d9a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FLAG_KINDS_OLD = ("repeat_poster", "competitor_mention", "no_follow_up")
FLAG_KINDS_NEW = (*FLAG_KINDS_OLD, "source_row_removed")

# ---- FROZEN copies (sales0.linkedin_url, sheetsync.values) -------------------------------------------
_KINDS = ("in", "company", "school", "pub")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def canonical_linkedin(url: str | None) -> tuple[str, str] | None:
    if not isinstance(url, str) or not url.strip():
        return None
    text = url.strip().split()[0]
    if text.startswith("//"):
        text = "https:" + text
    if text.startswith("/"):
        return None
    if not _SCHEME.match(text):
        text = "https://" + text
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None
    segments = [s for s in (parts.path or "/").split("/") if s]
    if len(segments) < 2 or segments[0].lower() not in _KINDS:
        return None
    slug = unicodedata.normalize("NFC", unquote(segments[1])).strip().casefold()
    return (segments[0].lower(), slug) if slug else None


def key_of(url: str | None) -> str | None:
    found = canonical_linkedin(url)
    if found is None:
        return None
    kind, slug = found
    return slug if kind == "in" else f"{kind}/{slug}"


def stable_hash(*parts: str, length: int = 24) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()[:length]


def norm_key_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def new_key(old_key: str, fields: dict) -> str:
    body = key_of(fields.get("linkedin_url"))
    if body:
        return f"li:{body}"
    return "nc:" + stable_hash(norm_key_text(fields.get("name")), norm_key_text(fields.get("company")))


# ---- schema helper ------------------------------------------------------------------------------------
def _check_sql(values: tuple[str, ...]) -> str:
    return "kind IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _set_flag_kinds(values: tuple[str, ...]) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.drop_constraint("ck_flags_kind", "flags", type_="check")
        op.create_check_constraint("ck_flags_kind", "flags", _check_sql(values))
        return
    before = bind.execute(sa.text("SELECT COUNT(*) FROM flags")).scalar_one()
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
        try:
            with op.batch_alter_table("flags", recreate="always") as batch:
                batch.drop_constraint(op.f("ck_flags_kind"), type_="check")
                batch.create_check_constraint("kind", sa.text(_check_sql(values)))
            bad = bind.execute(sa.text("PRAGMA foreign_key_check(flags)")).fetchall()
            if bad:
                raise RuntimeError(f"foreign_key_check found {len(bad)} violation(s) after rebuilding flags")
            names = {i["name"] for i in sa.inspect(bind).get_indexes("flags")}
            if "uq_flags_active" not in names:
                op.execute("CREATE UNIQUE INDEX uq_flags_active ON flags (entity_type, entity_id, kind) WHERE cleared_at IS NULL")
            if "ix_flags_kind_cleared_at" not in names:
                op.execute("CREATE INDEX ix_flags_kind_cleared_at ON flags (kind, cleared_at)")
        finally:
            op.execute("PRAGMA foreign_keys=ON")
    after = bind.execute(sa.text("SELECT COUNT(*) FROM flags")).scalar_one()
    if before != after:
        raise RuntimeError(f"flags rebuild changed the row count: {before} -> {after}")


def _fields(raw: object) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _rekey(compute) -> None:  # type: ignore[no-untyped-def]
    """Rewrite ``li:`` / ``nc:`` person keys with ``compute(old_key, fields, row)``; collisions keep ``row:<hash>``."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, key, sheet_id, gid, row_hash, fields FROM sheet_sources WHERE entity = 'prospect' ORDER BY id")
    ).fetchall()
    taken = {(r.sheet_id, r.gid, r.key) for r in rows}
    for r in rows:
        if not (r.key.startswith("li:") or r.key.startswith("nc:")):
            continue
        target = compute(r.key, _fields(r.fields), r)
        if target is None or target == r.key:
            continue
        if (r.sheet_id, r.gid, target) in taken:
            target = "row:" + r.row_hash
            if target == r.key or (r.sheet_id, r.gid, target) in taken:
                continue
        taken.discard((r.sheet_id, r.gid, r.key))
        taken.add((r.sheet_id, r.gid, target))
        bind.execute(sa.text("UPDATE sheet_sources SET key = :k WHERE id = :i"), {"k": target[:200], "i": r.id})


def upgrade() -> None:
    _rekey(lambda old, fields, _r: new_key(old, fields) if old.startswith("li:") else None)
    _set_flag_kinds(FLAG_KINDS_NEW)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM flags WHERE kind = 'source_row_removed'"))

    def back(old: str, _fields_: dict, _r) -> str | None:  # type: ignore[no-untyped-def]
        m = re.match(r"^li:(?:company|school|pub)/(.+)$", old)
        return f"li:{m.group(1)}" if m else None

    _rekey(back)
    _set_flag_kinds(FLAG_KINDS_OLD)
