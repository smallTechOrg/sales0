"""CRM feedback slice: entity characteristics, company matching keys, tracked-person note.

* ``characteristics`` - the editable registry (seeded: Training provider, Partner, Outreach client; idempotent by
  ``name_key`` / ``system_key``).
* ``entity_characteristics`` - (characteristic, entity_type ``prospect`` | ``client``, entity_id); unique per triple.
  A lead or a won client reads its prospect's rows, so a characteristic survives Prospect -> Lead -> Client.
* ``companies.domain_key`` / ``companies.linkedin_key`` (indexed) - matching keys besides the name, BACKFILLED from
  ``website`` / ``linkedin_url`` with frozen copies of ``services.crm._keys.domain_key`` / ``company_linkedin_key``.
* ``linkedin_profiles.watch_note`` - the optional note of "Add person to track".

Plain CREATE TABLE / nullable ADD COLUMN (no table rebuild; works on SQLite and PostgreSQL), retry-safe.

Revision ID: q12_traits_9d2c4a71
Revises: p11_rawcapture_a4d9c7e2
Create Date: 2026-10-29
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

import sqlalchemy as sa
from alembic import op

revision: str = "q12_traits_9d2c4a71"
down_revision: str | Sequence[str] | None = "p11_rawcapture_a4d9c7e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED = (("Training provider", "training_provider", 1), ("Partner", "partner", 2), ("Outreach client", "outreach_client", 3))

# ----------------------------------------------------------------------------- FROZEN helpers (never import app code)
_URL_LIKE = re.compile(r"^(?:https?://|www\.)", re.IGNORECASE)
_TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|trk|trackingid|originalsubdomain)", re.IGNORECASE)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
GENERIC_HOSTS = frozenset({
    "linkedin.com", "facebook.com", "fb.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "youtu.be",
    "google.com", "sites.google.com", "docs.google.com", "forms.gle", "gmail.com", "yahoo.com", "outlook.com",
    "hotmail.com", "wa.me", "whatsapp.com", "chat.whatsapp.com", "bit.ly", "lnkd.in", "t.me", "medium.com",
    "github.com", "wordpress.com", "blogspot.com", "wixsite.com", "linktr.ee",
})


def _normalize_url(text: str | None) -> str | None:
    t = (text or "").strip()
    if not t:
        return None
    t = t.split()[0]
    if not _URL_LIKE.match(t):
        return None
    if t.lower().startswith("www."):
        t = "https://" + t
    try:
        parts = urlsplit(t)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return None
    path = parts.path.rstrip("/")
    query = "&".join(sorted(p for p in parts.query.split("&") if p and not _TRACKING.match(p)))
    out = f"{host}{path}"
    return f"{out}?{query}" if query else out


def _website_host(text: str | None) -> str | None:
    t = (text or "").strip()
    if not t:
        return None
    t = t.split()[0]
    if "." not in t or "@" in t:
        return None
    if not _URL_LIKE.match(t):
        t = "https://" + t
    key = _normalize_url(t)
    return key.split("/", 1)[0].split("?", 1)[0] if key else None


def domain_key(website: str | None) -> str | None:
    host = _website_host(website)
    if not host or host in GENERIC_HOSTS or "." not in host:
        return None
    return host[:200]


def company_linkedin_key(url: str | None) -> str | None:
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
    if len(segments) < 2 or segments[0].lower() != "company":
        return None
    slug = unicodedata.normalize("NFC", unquote(segments[1])).strip().casefold()
    return f"company/{slug}"[:200] if slug else None


# ----------------------------------------------------------------------------- steps
def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _cols(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables()
    if "characteristics" not in tables:
        op.create_table(
            "characteristics",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(60), nullable=False),
            sa.Column("name_key", sa.String(60), nullable=False),
            sa.Column("system_key", sa.String(30), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_characteristics")),
            sa.UniqueConstraint("name_key", name=op.f("uq_characteristics_name_key")),
            sa.UniqueConstraint("system_key", name=op.f("uq_characteristics_system_key")),
        )
    if "entity_characteristics" not in tables:
        op.create_table(
            "entity_characteristics",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("characteristic_id", sa.Integer(), nullable=False),
            sa.Column("entity_type", sa.String(10), nullable=False),
            sa.Column("entity_id", sa.Integer(), nullable=False),
            sa.Column("actor", sa.String(80), nullable=False, server_default="system"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_characteristics")),
            sa.ForeignKeyConstraint(
                ["characteristic_id"], ["characteristics.id"], ondelete="CASCADE",
                name=op.f("fk_entity_characteristics_characteristic_id_characteristics"),
            ),
            sa.UniqueConstraint("characteristic_id", "entity_type", "entity_id", name="uq_entity_characteristics_entity"),
            sa.CheckConstraint("entity_type IN ('prospect', 'client')", name=op.f("ck_entity_characteristics_entity_type")),
        )
        op.create_index("ix_entity_characteristics_entity", "entity_characteristics", ["entity_type", "entity_id"])
    # seed (idempotent by name_key / system_key)
    stamp = datetime.now(UTC)
    if bind.dialect.name == "sqlite":
        stamp = stamp.replace(tzinfo=None)
    have = set(bind.execute(sa.text("SELECT name_key FROM characteristics")).scalars())
    have_sys = set(bind.execute(sa.text("SELECT system_key FROM characteristics WHERE system_key IS NOT NULL")).scalars())
    for name, key, order in SEED:
        if name.lower() in have or key in have_sys:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO characteristics (name, name_key, system_key, active, sort_order, created_at, updated_at) "
                "VALUES (:n, :k, :s, :a, :o, :t, :t)"
            ),
            {"n": name, "k": name.lower(), "s": key, "a": True, "o": order, "t": stamp},
        )
    # companies matching keys + backfill
    cols = _cols("companies")
    for name in ("domain_key", "linkedin_key"):
        if name not in cols:
            op.add_column("companies", sa.Column(name, sa.String(200), nullable=True))
    idx = _indexes("companies")
    for name in ("domain_key", "linkedin_key"):
        if f"ix_companies_{name}" not in idx:
            op.create_index(f"ix_companies_{name}", "companies", [name])
    rows = bind.execute(sa.text("SELECT id, website, linkedin_url, domain_key, linkedin_key FROM companies")).all()
    for cid, website, linkedin, dk, lk in rows:
        new_dk, new_lk = domain_key(website), company_linkedin_key(linkedin)
        if new_dk != dk or new_lk != lk:
            bind.execute(
                sa.text("UPDATE companies SET domain_key = :d, linkedin_key = :l WHERE id = :i"), {"d": new_dk, "l": new_lk, "i": cid}
            )
    if "watch_note" not in _cols("linkedin_profiles"):
        op.add_column("linkedin_profiles", sa.Column("watch_note", sa.String(300), nullable=True))


def downgrade() -> None:
    if "watch_note" in _cols("linkedin_profiles"):
        op.drop_column("linkedin_profiles", "watch_note")
    idx = _indexes("companies")
    for name in ("linkedin_key", "domain_key"):
        if f"ix_companies_{name}" in idx:
            op.drop_index(f"ix_companies_{name}", table_name="companies")
    cols = _cols("companies")
    for name in ("linkedin_key", "domain_key"):
        if name in cols:
            op.drop_column("companies", name)
    tables = _tables()
    if "entity_characteristics" in tables:
        op.drop_index("ix_entity_characteristics_entity", table_name="entity_characteristics")
        op.drop_table("entity_characteristics")
    if "characteristics" in tables:
        op.drop_table("characteristics")
