"""Migration 0003_phase1_4_seen_posts on a POPULATED 0002 SQLite file (data.md -> Phase 1.4).

Counts and FKs survive; exactly the never-touched AI discards move to ``seen_posts``; the frozen
helpers equal the app's; the migrated schema equals the ORM models.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from sales0.config import REPO_ROOT
from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, current_revision, head_revision
from tests.integration.db._drift import assert_inline_fks_cascade, drift

MIGRATION_FILE = REPO_ROOT / "migrations" / "versions" / "0003_phase1_4_seen_posts.py"

T0 = datetime(2026, 10, 20, 9, 0, 0)  # naive: SQLite stores UTC without an offset

P1_TEXT = "We need a corporate AI training partner for 40 managers in Pune. Budget approved. #hiring"
P2_TEXT = (
    "Join our free AI webinar next week! Register at https://example.com/webinar now"
)
P3_TEXT = "Thought leadership: why agentic AI will change everything about work in the next decade."
P4_TEXT = (
    "Agency here: we can staff your AI trainer needs, DM us for rates and availability."
)
P5_TEXT = (
    "Looking for an AI consultant to build a RAG chatbot. Reach me at priya@example.com"
)
P6_TEXT = "Hiring: GenAI workshop facilitator for engineering teams, Bengaluru, two days in March."


def _load_migration():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "migration_0003_under_test", MIGRATION_FILE
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_enquiry(conn: sa.Connection, **v: object) -> int:
    base = {
        "source": "linkedin_post",
        "date_added": T0,
        "review_status": "pending_review",
        "triage_status": "done",
        "ai_verdict": None,
    }
    cols = {**base, **v}
    names = ", ".join(cols)
    params = ", ".join(f":{k}" for k in cols)
    result = conn.execute(
        sa.text(f"INSERT INTO enquiries ({names}) VALUES ({params})"), cols
    )
    return int(result.lastrowid)


def _hash(label: str) -> str:
    return (label * 64)[:64]


@pytest.fixture
def populated(tmp_path: Path) -> dict[str, object]:
    """A 0002 database with six enquiries covering every backfill branch."""
    url = f"sqlite:///{(tmp_path / 'p.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0002_phase1_discovery")
    engine = build_engine(url)
    ids: dict[str, int] = {}
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO keywords (text, text_key, active, created_at) VALUES ('corporate ai training', 'corporate ai training', 1, :t)"
            ),
            {"t": T0},
        )
        conn.execute(
            sa.text(
                "INSERT INTO scrape_runs (id, trigger, mode, status, started_at) VALUES (1, 'manual', 'fixture', 'succeeded', :t)"
            ),
            {"t": T0},
        )
        ids["p1"] = _insert_enquiry(
            conn,
            content_hash=_hash("1"),
            raw_text=P1_TEXT,
            author_name="Priya Menon",
            author_headline="HR Head",
            author_profile_url="https://www.linkedin.com/in/Priya-Menon/?miniProfileUrn=x",
            source_url="https://www.linkedin.com/feed/update/urn:li:activity:1/",
            review_status="accepted",
            stage="prospect_new",
            ai_verdict="genuine",
            ai_reason="buyer",
            ai_extraction='{"verdict": "genuine"}',
        )
        ids["p2"] = _insert_enquiry(
            conn,
            content_hash=_hash("2"),
            raw_text=P2_TEXT,
            author_name="Bulk Author 1",
            author_profile_url="https://www.linkedin.com/in/bulk-author-1/",
            ai_verdict="discard",
            ai_reason="event",
            ai_poster_type="event_promoter",
            ai_extraction='{"verdict": "discard"}',
        )
        ids["p3"] = _insert_enquiry(
            conn,
            content_hash=_hash("3"),
            raw_text=P3_TEXT,
            author_name="Bulk Author 1",
            author_profile_url="https://www.linkedin.com/in/bulk-author-1/",
            ai_verdict="discard",
            ai_reason="thought leadership",
        )
        ids["p4"] = _insert_enquiry(
            conn,
            content_hash=_hash("4"),
            raw_text=P4_TEXT,
            author_name="Agency Co",
            author_profile_url="https://www.linkedin.com/company/agency-co/",
            review_status="discarded",
            ai_verdict="discard",
            ai_reason="agency",
        )
        ids["p5"] = _insert_enquiry(
            conn,
            content_hash=_hash("5"),
            raw_text=P5_TEXT,
            author_name="No Slug",
            author_profile_url=None,
            triage_status="failed",
            triage_error="LLM timeout",
        )
        ids["p6"] = _insert_enquiry(
            conn,
            content_hash=_hash("6"),
            raw_text=P6_TEXT,
            author_name="Hiring Manager",
            author_profile_url="https://www.linkedin.com/in/hiring-manager/",
            ai_verdict="genuine",
            ai_reason="buyer",
        )
        for key, ref in (
            ("p1", "urn:li:activity:1"),
            ("p2", "ck:2"),
            ("p3", "ck:3"),
            ("p4", "ck:4"),
            ("p5", "ck:5"),
            ("p6", "ck:6a"),
            ("p6", "urn:li:activity:6"),
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO enquiry_sources (enquiry_id, source_ref, source_url, first_seen_run_id, first_seen_at) VALUES (:e, :r, NULL, 1, :t)"
                ),
                {"e": ids[key], "r": ref, "t": T0},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO enquiry_keyword_hits (enquiry_id, run_id, keyword_id, keyword_text, seen_at) VALUES (:e, 1, 1, 'corporate ai training', :t) ON CONFLICT DO NOTHING"
                ),
                {"e": ids[key], "t": T0},
            )
        conn.execute(
            sa.text(
                "INSERT INTO triage_decisions (enquiry_id, decision, ai_verdict, is_override, decided_at) VALUES (:e, 'discard', 'discard', 0, :t)"
            ),
            {"e": ids["p4"], "t": T0},
        )
        for key, cost in (
            ("p1", 100),
            ("p2", 200),
            ("p3", 300),
            ("p4", 400),
            ("p6", 500),
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO llm_calls (feature, model, enquiry_id, run_id, cost_usd_micros, status, created_at) VALUES ('triage', 'claude-haiku-5-5', :e, 1, :c, 'ok', :t)"
                ),
                {"e": ids[key], "c": cost, "t": T0},
            )
    engine.dispose()
    return {"url": url, "cfg": cfg, "ids": ids}


def _rows(url: str, sql: str, **params: object) -> list[sa.Row]:
    engine = build_engine(url)
    try:
        with engine.connect() as conn:
            return list(conn.execute(sa.text(sql), params).all())
    finally:
        engine.dispose()


def test_upgrade_moves_exactly_the_never_touched_ai_discards(
    populated: dict[str, object],
) -> None:
    url, cfg, ids = populated["url"], populated["cfg"], populated["ids"]  # type: ignore[assignment]
    before = datetime.now(UTC).replace(tzinfo=None)
    command.upgrade(cfg, "head")  # type: ignore[arg-type]

    engine = build_engine(url)  # type: ignore[arg-type]
    try:
        assert current_revision(engine) == head_revision()
    finally:
        engine.dispose()

    kept = {r[0] for r in _rows(url, "SELECT id FROM enquiries")}  # type: ignore[arg-type]
    assert kept == {ids["p1"], ids["p4"], ids["p5"], ids["p6"]}  # type: ignore[index]
    # moved enquiries lose sources and hits; kept ones keep theirs (p6 has two sources)
    src = dict(
        _rows(
            url, "SELECT enquiry_id, COUNT(*) FROM enquiry_sources GROUP BY enquiry_id"
        )
    )  # type: ignore[arg-type]
    assert src == {ids["p1"]: 1, ids["p4"]: 1, ids["p5"]: 1, ids["p6"]: 2}  # type: ignore[index]
    hits = {r[0] for r in _rows(url, "SELECT enquiry_id FROM enquiry_keyword_hits")}  # type: ignore[arg-type]
    assert hits == kept
    assert _rows(url, "SELECT COUNT(*) FROM triage_decisions")[0][0] == 1  # type: ignore[arg-type]

    seen = {
        r.fingerprint: r
        for r in _rows(url, "SELECT * FROM seen_posts ORDER BY id")  # type: ignore[arg-type]
    }
    assert len(seen) == 6  # one per original enquiry
    by_text = {r.snippet[:20]: r for r in seen.values()}
    moved_rows = [r for r in seen.values() if r.enquiry_id is None]
    assert len(moved_rows) == 2
    for r in moved_rows:
        assert r.verdict == "discard" and r.triage_status == "done"
        assert r.raw_text in (P2_TEXT, P3_TEXT)
        purge = datetime.fromisoformat(str(r.raw_purge_after))
        assert (
            before + timedelta(days=13, hours=23)
            < purge
            < before + timedelta(days=14, hours=1)
        )
        assert r.raw_purged_at is None
    assert {r.reason for r in moved_rows} == {"event", "thought leadership"}
    assert any(r.poster_type == "event_promoter" and r.ai_result for r in moved_rows)
    assert by_text[P2_TEXT[:20]].snippet == P2_TEXT[:240]

    kept_rows = [r for r in seen.values() if r.enquiry_id is not None]
    assert {r.enquiry_id for r in kept_rows} == kept
    for r in kept_rows:
        assert (
            r.raw_text is None and r.raw_purge_after is None
        )  # the text lives on the enquiry
    p1 = next(r for r in kept_rows if r.enquiry_id == ids["p1"])  # type: ignore[index]
    assert (
        p1.verdict == "genuine"
        and p1.post_url == "https://www.linkedin.com/feed/update/urn:li:activity:1/"
    )
    assert (
        p1.source_ref == "urn:li:activity:1"
        and p1.keyword_text == "corporate ai training"
        and p1.first_run_id == 1
    )
    p5 = next(r for r in kept_rows if r.enquiry_id == ids["p5"])  # type: ignore[index]
    assert p5.triage_status == "failed" and p5.verdict is None and p5.profile_id is None
    p6 = next(r for r in kept_rows if r.enquiry_id == ids["p6"])  # type: ignore[index]
    assert p6.source_ref == "ck:6a" and p6.post_url is None

    # the enquiries' own links point back at their seen_posts row and profile
    links = _rows(
        url,
        "SELECT e.id, e.seen_post_id, e.author_profile_id, s.enquiry_id, s.profile_id FROM enquiries e JOIN seen_posts s ON s.id = e.seen_post_id",
    )  # type: ignore[arg-type]
    assert len(links) == 4
    for eid, _sp, ap, s_eid, s_pid in links:
        assert s_eid == eid and s_pid == ap
    assert [
        r[0]
        for r in _rows(
            url, "SELECT author_profile_id FROM enquiries WHERE id = :i", i=ids["p5"]
        )
    ] == [None]  # type: ignore[arg-type,index]

    # PRAGMA foreign_key_check is empty
    assert _rows(url, "PRAGMA foreign_key_check") == []  # type: ignore[arg-type]


def test_llm_calls_and_costs_are_preserved_with_seen_post_id(
    populated: dict[str, object],
) -> None:
    url, cfg, ids = populated["url"], populated["cfg"], populated["ids"]  # type: ignore[assignment]
    total_before = _rows(url, "SELECT COUNT(*), SUM(cost_usd_micros) FROM llm_calls")[0]  # type: ignore[arg-type]
    command.upgrade(cfg, "head")  # type: ignore[arg-type]
    assert tuple(
        _rows(url, "SELECT COUNT(*), SUM(cost_usd_micros) FROM llm_calls")[0]
    ) == tuple(total_before)  # type: ignore[arg-type]
    calls = {
        r.cost_usd_micros: r
        for r in _rows(
            url,
            "SELECT l.cost_usd_micros, l.enquiry_id, l.seen_post_id, s.enquiry_id AS s_eid, s.snippet FROM llm_calls l JOIN seen_posts s ON s.id = l.seen_post_id",
        )  # type: ignore[arg-type]
    }
    assert set(calls) == {
        100,
        200,
        300,
        400,
        500,
    }  # every call joined to a seen_posts row
    assert calls[200].enquiry_id is None and calls[200].snippet.startswith(
        "Join our free AI webinar"
    )
    assert calls[300].enquiry_id is None and calls[300].snippet.startswith(
        "Thought leadership"
    )
    assert calls[100].enquiry_id == ids["p1"] and calls[100].s_eid == ids["p1"]  # type: ignore[index]
    assert calls[400].enquiry_id == ids["p4"] and calls[500].enquiry_id == ids["p6"]  # type: ignore[index]


def test_profile_counters_and_identity(populated: dict[str, object]) -> None:
    url, cfg = populated["url"], populated["cfg"]  # type: ignore[assignment]
    command.upgrade(cfg, "head")  # type: ignore[arg-type]
    profiles = {r.slug: r for r in _rows(url, "SELECT * FROM linkedin_profiles")}  # type: ignore[arg-type]
    assert set(profiles) == {
        "in/priya-menon",
        "in/bulk-author-1",
        "company/agency-co",
        "in/hiring-manager",
    }
    priya = profiles["in/priya-menon"]
    assert (
        priya.kind == "person"
        and priya.profile_url == "https://www.linkedin.com/in/priya-menon/"
    )
    assert priya.display_name == "Priya Menon" and priya.headline == "HR Head"
    assert (priya.posts_seen, priya.relevant_count) == (1, 1)  # accepted
    assert priya.tracked == 1  # 0003 leaves it NULL; the p6 CRM backfill tracks the author of an accepted enquiry
    bulk = profiles["in/bulk-author-1"]
    assert (bulk.posts_seen, bulk.relevant_count) == (
        2,
        0,
    )  # two moved discards, none relevant
    agency = profiles["company/agency-co"]
    assert agency.kind == "company" and (agency.posts_seen, agency.relevant_count) == (
        1,
        0,
    )  # human-discarded, not genuine
    assert (
        profiles["in/hiring-manager"].posts_seen,
        profiles["in/hiring-manager"].relevant_count,
    ) == (1, 1)  # AI-genuine


def test_new_columns_default_to_zero_or_null_on_existing_rows(
    populated: dict[str, object],
) -> None:
    url, cfg = populated["url"], populated["cfg"]  # type: ignore[assignment]
    command.upgrade(cfg, "head")  # type: ignore[arg-type]
    kw = _rows(
        url,
        "SELECT group_name, suggested_key, scanned_total, relevant_total, last_scanned, last_relevant FROM keywords",
    )[0]  # type: ignore[arg-type]
    assert tuple(kw) == (None, None, 0, 0, 0, 0)
    run = _rows(
        url,
        "SELECT posts_relevant, posts_skipped_ai, permalinks_found FROM scrape_runs",
    )[0]  # type: ignore[arg-type]
    assert tuple(run) == (0, 0, 0)
    assert (
        _rows(url, "SELECT COUNT(*) FROM enquiries WHERE posted_relative IS NOT NULL")[
            0
        ][0]
        == 0
    )  # type: ignore[arg-type]


def test_migrated_populated_schema_has_no_drift_from_the_models(
    populated: dict[str, object],
) -> None:
    url, cfg = populated["url"], populated["cfg"]  # type: ignore[assignment]
    command.upgrade(cfg, "head")  # type: ignore[arg-type]
    engine = build_engine(url)  # type: ignore[arg-type]
    try:
        assert drift(engine) == []
        assert_inline_fks_cascade(engine)
    finally:
        engine.dispose()


def test_upgrade_on_an_empty_0002_database_is_a_no_op_backfill(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'empty.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0002_phase1_discovery")
    command.upgrade(cfg, "head")
    assert _rows(
        url,
        "SELECT (SELECT COUNT(*) FROM seen_posts), (SELECT COUNT(*) FROM linkedin_profiles)",
    )[0] == (0, 0)


def test_downgrade_to_0002_keeps_remaining_enquiries_and_upgrade_round_trips(
    populated: dict[str, object],
) -> None:
    url, cfg = populated["url"], populated["cfg"]  # type: ignore[assignment]
    command.upgrade(cfg, "head")  # type: ignore[arg-type]
    command.downgrade(cfg, "0002_phase1_discovery")  # type: ignore[arg-type]
    tables = {
        r[0] for r in _rows(url, "SELECT name FROM sqlite_master WHERE type='table'")
    }  # type: ignore[arg-type]
    assert not {"seen_posts", "linkedin_profiles"} & tables
    assert _rows(url, "SELECT COUNT(*) FROM enquiries")[0][0] == 4  # type: ignore[arg-type]
    cols = {r[1] for r in _rows(url, "PRAGMA table_info(enquiries)")}  # type: ignore[arg-type]
    assert not {"seen_post_id", "author_profile_id", "posted_relative"} & cols
    command.upgrade(cfg, "head")  # type: ignore[arg-type]
    assert _rows(url, "SELECT COUNT(*) FROM seen_posts")[0][0] == 4  # type: ignore[arg-type]  # only the 4 kept enquiries remain


# ---- frozen helpers vs the app -------------------------------------------------------
SAMPLES = [
    "Plain text about AI training.",
    "Visit https://example.com/x and www.foo.com/bar or lnkd.in/abc #hashtag #AI done",
    "ＦＵＬＬＷＩＤＴＨ   spaced\n\ttext with é and 日本語",
    "Read more … see more",
    "Trailing dots...",
    "",
]
URLS = [
    "https://www.linkedin.com/in/Priya-Menon-1/?x=1#frag",
    "https://www.linkedin.com/company/Acme/",
    "http://127.0.0.1:9/__fixtures/linkedin/in/bulk-author-001/",
    "https://www.linkedin.com/in/priya%2Dmenon/",
    "https://www.linkedin.com/feed/update/urn:li:activity:1/",
    "https://www.linkedin.com/search/results/content/?keywords=x",
    "not a url",
    "",
    None,
]


def test_frozen_helpers_equal_the_app_versions() -> None:
    from sales0.scraper.urls import link_kind_of, profile_slug
    from sales0.services import dedupe

    m = _load_migration()
    for text in SAMPLES:
        assert m.normalize_text(text) == dedupe.normalize_text(text)
        assert m.strip_ellipsis(text) == dedupe.strip_ellipsis(text)
        assert m.content_hash(text) == dedupe.content_hash(text)
    for url in URLS:
        assert m.profile_slug(url) == profile_slug(url)
        assert m.is_post_link(url) == (link_kind_of(url) == "post")


def test_the_frozen_fingerprint_equals_services_seen_posts_fingerprint() -> None:
    from sales0.services import seen_posts

    m = _load_migration()
    for slug in (None, "", "in/priya-menon", "company/acme"):
        for text in SAMPLES:
            assert m.fingerprint(slug, text) == seen_posts.fingerprint(slug, text)
