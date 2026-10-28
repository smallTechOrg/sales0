"""Phase 1.4 thorough, idempotent discovery: linkedin_profiles + seen_posts, yield/funnel
counters, and the backfill that moves never-reviewed AI discards out of ``enquiries``.

All changes to Phase-1 tables are in-place ``ADD COLUMN`` (no table rebuild: ``scrape_runs`` and
``enquiries`` are referenced by foreign keys). The helpers below are FROZEN copies of
``services.dedupe.normalize_text`` / ``strip_ellipsis``, ``scraper.urls.profile_slug`` and the
``services.seen_posts.fingerprint`` formula; ``tests/integration/db/test_migration_0003.py``
asserts they equal the app versions. Never import app code here.

Revision ID: 0003_phase1_4_seen_posts
Revises: 0002_phase1_discovery
Create Date: 2026-10-28
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

import sqlalchemy as sa
from alembic import op

revision: str = "0003_phase1_4_seen_posts"
down_revision: str | Sequence[str] | None = "0002_phase1_discovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen enum values at this revision.
SEEN_TRIAGE_STATUSES = ("pending", "done", "failed")
VERDICTS = ("genuine", "discard")
PROFILE_KINDS = ("person", "company")

RAW_RETENTION_DAYS = 14
FINGERPRINT_CHARS = 300
SNIPPET_CHARS = 240

# ------------------------------------------------------------------ frozen helpers
_URL = re.compile(
    r"(?:https?://|www\.)\S+|(?<![\w@.])(?:[a-z0-9-]+\.)+[a-z]{2,}/\S*",
    re.IGNORECASE,
)
_HASHTAG = re.compile(r"#[^\s#]+")
_WS = re.compile(r"\s+")
_TRAILING_MORE = re.compile(r"\s*(?:…|\.\.\.)\s*(?:see\s+)?more\s*$", re.IGNORECASE)
_TRAILING_ELLIPSIS = re.compile(r"\s*(?:…|\.\.\.)\s*$")
_PROFILE_PATH = re.compile(r"^/(in|company)/([^/?#]+)")
_FIXTURE_PREFIX = "/__fixtures/linkedin"


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "").lower()
    value = _URL.sub(" ", value)
    value = _HASHTAG.sub(" ", value)
    return _WS.sub(" ", value).strip()


def strip_ellipsis(text: str) -> str:
    value = _TRAILING_MORE.sub("", text or "")
    return _TRAILING_ELLIPSIS.sub("", value)


def _strip_fixture_prefix(path: str) -> str:
    if path == _FIXTURE_PREFIX or path.startswith(_FIXTURE_PREFIX + "/"):
        return path[len(_FIXTURE_PREFIX) :] or "/"
    return path


def _path_of(url: str) -> str | None:
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    return _strip_fixture_prefix(path or "/")


def profile_slug(url: str | None) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    path = _path_of(url.strip())
    if path is None:
        return None
    match = _PROFILE_PATH.match(path)
    if not match:
        return None
    slug = unquote(match.group(2)).strip().lower()
    return f"{match.group(1)}/{slug}" if slug else None


def is_post_link(url: str | None) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    path = _path_of(url.strip())
    return path is not None and ("/feed/update/" in path or path.startswith("/posts/"))


def fingerprint(author_slug: str | None, text: str) -> str:
    return hashlib.sha256(
        ((author_slug or "") + "\n" + normalize_text(text)).encode("utf-8")
    ).hexdigest()


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ DDL helpers
def _in(
    column: str, values: tuple[str, ...], name: str, *, nullable: bool = False
) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    expr = f"{column} IN ({allowed})"
    if nullable:
        expr = f"{column} IS NULL OR {expr}"
    return sa.CheckConstraint(expr, name=name)


def _ts(name: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _counter_col(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default="0")


def _add_counters(table: str, names: Sequence[str]) -> None:
    for name in names:
        op.add_column(table, _counter_col(name))


def _add_fk_column(
    table: str, column: str, target: str, *, index: bool = False
) -> None:
    """ADD COLUMN with an inline REFERENCES clause (SQLite cannot add a constraint afterwards)."""
    op.execute(
        f"ALTER TABLE {table} ADD COLUMN {column} INTEGER REFERENCES {target}(id) ON DELETE SET NULL"
    )
    if index:
        op.create_index(f"ix_{table}_{column}", table, [column])


def upgrade() -> None:
    op.create_table(
        "linkedin_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(200), nullable=False, unique=True),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("profile_url", sa.String(500), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("headline", sa.String(500), nullable=True),
        _ts("first_seen_at", nullable=False),
        _ts("last_seen_at", nullable=False),
        _counter_col("posts_seen"),
        _counter_col("relevant_count"),
        sa.Column("tracked", sa.Boolean(), nullable=True),
        _in("kind", PROFILE_KINDS, "kind"),
    )

    op.create_table(
        "seen_posts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("content_hash", sa.String(64), nullable=False, index=True),
        sa.Column("text_prefix", sa.String(300), nullable=False, server_default=""),
        sa.Column("source_ref", sa.String(200), nullable=True, unique=True),
        sa.Column(
            "profile_id",
            sa.Integer(),
            sa.ForeignKey("linkedin_profiles.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("author_name", sa.String(200), nullable=True),
        sa.Column("author_headline", sa.String(500), nullable=True),
        sa.Column("keyword_text", sa.String(100), nullable=False),
        sa.Column(
            "first_run_id",
            sa.Integer(),
            sa.ForeignKey("scrape_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "triage_status", sa.String(10), nullable=False, server_default="pending"
        ),
        sa.Column("triage_error", sa.String(300), nullable=True),
        sa.Column("verdict", sa.String(10), nullable=True),
        sa.Column("reason", sa.String(160), nullable=True),
        sa.Column("poster_type", sa.String(40), nullable=True),
        sa.Column("ai_result", sa.JSON(), nullable=True),
        sa.Column("snippet", sa.String(240), nullable=False, server_default=""),
        sa.Column("raw_text", sa.Text(), nullable=True),
        _ts("raw_purge_after"),
        _ts("raw_purged_at"),
        sa.Column("post_url", sa.String(500), nullable=True),
        sa.Column("posted_relative", sa.String(30), nullable=True),
        sa.Column(
            "enquiry_id",
            sa.Integer(),
            sa.ForeignKey("enquiries.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        _ts("rescued_at"),
        _ts("first_seen_at", nullable=False),
        _ts("last_seen_at", nullable=False),
        sa.Column("times_seen", sa.Integer(), nullable=False, server_default="1"),
        sa.Index("ix_seen_posts_verdict_first_seen", "verdict", "first_seen_at"),
        _in("triage_status", SEEN_TRIAGE_STATUSES, "triage_status"),
        _in("verdict", VERDICTS, "verdict", nullable=True),
    )

    # --- in-place ADD COLUMN on Phase-1 tables -------------------------------------
    op.add_column("keywords", sa.Column("group_name", sa.String(40), nullable=True))
    op.add_column("keywords", sa.Column("suggested_key", sa.String(60), nullable=True))
    op.create_index(
        "uq_keywords_suggested_key", "keywords", ["suggested_key"], unique=True
    )
    _add_counters(
        "keywords", ("scanned_total", "relevant_total", "last_scanned", "last_relevant")
    )

    _add_counters(
        "scrape_runs", ("posts_relevant", "posts_skipped_ai", "permalinks_found")
    )
    _add_counters(
        "scrape_run_keywords",
        (
            "posts_relevant",
            "posts_skipped_ai",
            "permalinks_found",
            "scrolls",
            "cost_usd_micros",
        ),
    )

    op.add_column(
        "enquiries", sa.Column("posted_relative", sa.String(30), nullable=True)
    )
    _add_fk_column("enquiries", "author_profile_id", "linkedin_profiles")
    _add_fk_column("enquiries", "seen_post_id", "seen_posts")
    _add_fk_column("llm_calls", "seen_post_id", "seen_posts", index=True)

    _backfill(op.get_bind())


# ------------------------------------------------------------------ backfill
def _backfill(bind: sa.engine.Connection) -> None:
    meta = sa.MetaData()
    names = (
        "enquiries",
        "enquiry_sources",
        "enquiry_keyword_hits",
        "triage_decisions",
        "llm_calls",
        "linkedin_profiles",
        "seen_posts",
    )
    t = {n: sa.Table(n, meta, autoload_with=bind) for n in names}
    enq, src, hits, dec, llm, prof, seen = (t[n] for n in names)

    now = datetime.now(UTC)
    stamp = now.replace(tzinfo=None) if bind.dialect.name == "sqlite" else now
    purge_after = stamp + timedelta(days=RAW_RETENTION_DAYS)

    decided = {row[0] for row in bind.execute(sa.select(dec.c.enquiry_id).distinct())}
    first_src: dict[int, tuple[str, int | None]] = {}
    for enquiry_id, ref, run_id in bind.execute(
        sa.select(src.c.enquiry_id, src.c.source_ref, src.c.first_seen_run_id).order_by(
            src.c.id
        )
    ):
        first_src.setdefault(enquiry_id, (ref, run_id))
    first_kw: dict[int, str] = {}
    for enquiry_id, text in bind.execute(
        sa.select(hits.c.enquiry_id, hits.c.keyword_text).order_by(hits.c.id)
    ):
        first_kw.setdefault(enquiry_id, text)

    profile_ids: dict[str, int] = {}
    used_fp: set[str] = set()
    used_ref: set[str] = set()
    moved: list[int] = []

    rows = bind.execute(sa.select(enq).order_by(enq.c.id)).mappings().all()
    for e in rows:
        slug = profile_slug(e["author_profile_url"])
        profile_id: int | None = None
        if slug is not None:
            profile_id = profile_ids.get(slug)
            if profile_id is None:
                result = bind.execute(
                    prof.insert().values(
                        slug=slug,
                        kind="person" if slug.startswith("in/") else "company",
                        profile_url=f"https://www.linkedin.com/{slug}/",
                        display_name=(e["author_name"] or None)
                        and e["author_name"][:200],
                        headline=(e["author_headline"] or None)
                        and e["author_headline"][:500],
                        first_seen_at=e["date_added"],
                        last_seen_at=e["date_added"],
                        posts_seen=0,
                        relevant_count=0,
                        tracked=None,
                    )
                )
                profile_id = int(result.inserted_primary_key[0])
                profile_ids[slug] = profile_id
            else:
                values: dict[str, object] = {"last_seen_at": e["date_added"]}
                if e["author_name"]:
                    values["display_name"] = e["author_name"][:200]
                if e["author_headline"]:
                    values["headline"] = e["author_headline"][:500]
                bind.execute(
                    prof.update().where(prof.c.id == profile_id).values(**values)
                )

        text = e["raw_text"] or ""
        fp = fingerprint(slug, text)
        if (
            fp in used_fp
        ):  # same author + same normalised text (e.g. both empty): keep rows distinct
            fp = hashlib.sha256(f"{fp}\nenquiry:{e['id']}".encode()).hexdigest()
        used_fp.add(fp)

        ref, run_id = first_src.get(e["id"], (None, None))
        if ref is not None and ref in used_ref:
            ref = None
        if ref is not None:
            used_ref.add(ref)

        never_touched = (
            e["ai_verdict"] == "discard"
            and e["review_status"] == "pending_review"
            and e["id"] not in decided
        )
        verdict = e["ai_verdict"]
        post_url = e["source_url"][:500] if is_post_link(e["source_url"]) else None
        result = bind.execute(
            seen.insert().values(
                fingerprint=fp,
                content_hash=content_hash(text)
                if normalize_text(text)
                else e["content_hash"],
                text_prefix=normalize_text(strip_ellipsis(text))[:FINGERPRINT_CHARS],
                source_ref=ref,
                profile_id=profile_id,
                author_name=(e["author_name"] or None) and e["author_name"][:200],
                author_headline=(e["author_headline"] or None)
                and e["author_headline"][:500],
                keyword_text=(first_kw.get(e["id"]) or "")[:100],
                first_run_id=run_id,
                triage_status=e["triage_status"],
                triage_error=e["triage_error"],
                verdict=verdict,
                reason=e["ai_reason"],
                poster_type=e["ai_poster_type"],
                ai_result=e["ai_extraction"],
                snippet=_WS.sub(" ", text).strip()[:SNIPPET_CHARS],
                raw_text=text if never_touched else None,
                raw_purge_after=purge_after if never_touched else None,
                raw_purged_at=None,
                post_url=post_url,
                posted_relative=None,
                enquiry_id=None if never_touched else e["id"],
                rescued_at=None,
                first_seen_at=e["date_added"],
                last_seen_at=e["date_added"],
                times_seen=1,
            )
        )
        seen_id = int(result.inserted_primary_key[0])

        bind.execute(
            llm.update().where(llm.c.enquiry_id == e["id"]).values(seen_post_id=seen_id)
        )
        if never_touched:
            moved.append(e["id"])
            bind.execute(
                llm.update().where(llm.c.enquiry_id == e["id"]).values(enquiry_id=None)
            )
        else:
            bind.execute(
                enq.update()
                .where(enq.c.id == e["id"])
                .values(author_profile_id=profile_id, seen_post_id=seen_id)
            )

    if moved:
        for chunk_start in range(0, len(moved), 500):
            chunk = moved[chunk_start : chunk_start + 500]
            bind.execute(src.delete().where(src.c.enquiry_id.in_(chunk)))
            bind.execute(hits.delete().where(hits.c.enquiry_id.in_(chunk)))
            bind.execute(enq.delete().where(enq.c.id.in_(chunk)))

    # Profile counters: posts_seen = seen_posts rows; relevant_count = remaining genuine/accepted enquiries.
    seen_counts = dict(
        bind.execute(
            sa.select(seen.c.profile_id, sa.func.count())
            .where(seen.c.profile_id.is_not(None))
            .group_by(seen.c.profile_id)
        ).all()
    )
    relevant_counts = dict(
        bind.execute(
            sa.select(enq.c.author_profile_id, sa.func.count())
            .where(
                enq.c.author_profile_id.is_not(None),
                sa.or_(
                    enq.c.ai_verdict == "genuine", enq.c.review_status == "accepted"
                ),
            )
            .group_by(enq.c.author_profile_id)
        ).all()
    )
    for pid in profile_ids.values():
        bind.execute(
            prof.update()
            .where(prof.c.id == pid)
            .values(
                posts_seen=int(seen_counts.get(pid, 0)),
                relevant_count=int(relevant_counts.get(pid, 0)),
            )
        )


# ------------------------------------------------------------------ downgrade
@contextmanager
def _children_safe(ignore: Sequence[str] = ()) -> Iterator[None]:
    """Run table rebuilds without cascading into child rows (p6 four-step recipe).

    SQLite's batch rebuild drops the old table; under ``foreign_keys=ON`` that implicit DELETE cascades to
    (or NULLs) the child rows. ``PRAGMA foreign_keys`` is a no-op inside a transaction, hence the autocommit
    block. Afterwards ``foreign_key_check`` must not report more violations than before and every surviving
    table keeps its row count, so a regression fails loudly instead of silently losing data.
    """
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        yield
        return
    tables = [t for t in sa.inspect(bind).get_table_names() if t not in ignore]
    before = {t: bind.execute(sa.text(f'SELECT COUNT(*) FROM "{t}"')).scalar_one() for t in tables}
    bad_before = len(bind.execute(sa.text("PRAGMA foreign_key_check")).fetchall())
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
        try:
            yield
            bad = bind.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
            if len(bad) > bad_before:
                raise RuntimeError(f"foreign_key_check found {len(bad) - bad_before} new violation(s) after the rebuild")
        finally:
            op.execute("PRAGMA foreign_keys=ON")
    live = set(sa.inspect(bind).get_table_names())
    after = {t: bind.execute(sa.text(f'SELECT COUNT(*) FROM "{t}"')).scalar_one() for t in tables if t in live}
    if after != {t: before[t] for t in after}:
        raise RuntimeError(f"downgrade rebuild changed child row counts: {before} -> {after}")


def downgrade() -> None:
    """Structure only: enquiries moved to ``seen_posts`` by the upgrade are not restored."""
    with _children_safe(ignore=("seen_posts", "linkedin_profiles")):
        _downgrade_structure()


def _downgrade_structure() -> None:
    with op.batch_alter_table("llm_calls") as batch:
        batch.drop_index("ix_llm_calls_seen_post_id")
        batch.drop_column("seen_post_id")
    with op.batch_alter_table("enquiries") as batch:
        batch.drop_column("seen_post_id")
        batch.drop_column("author_profile_id")
        batch.drop_column("posted_relative")
    with op.batch_alter_table("scrape_run_keywords") as batch:
        for name in (
            "cost_usd_micros",
            "scrolls",
            "permalinks_found",
            "posts_skipped_ai",
            "posts_relevant",
        ):
            batch.drop_column(name)
    with op.batch_alter_table("scrape_runs") as batch:
        for name in ("permalinks_found", "posts_skipped_ai", "posts_relevant"):
            batch.drop_column(name)
    op.drop_index("uq_keywords_suggested_key", table_name="keywords")
    with op.batch_alter_table("keywords") as batch:
        for name in (
            "last_relevant",
            "last_scanned",
            "relevant_total",
            "scanned_total",
            "suggested_key",
            "group_name",
        ):
            batch.drop_column(name)
    op.drop_table("seen_posts")
    op.drop_table("linkedin_profiles")
