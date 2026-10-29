"""``sales0 pull-data`` end to end, in-process: a seeded hosted-mode app (SQLite here; PostgreSQL when
SALES0_TEST_POSTGRES_URL is set) -> ``/api/worker/export/*`` -> the real ``pull`` into a fresh local SQLite file."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, text

from sales0.db.base import Base
from sales0.db.engine import build_engine, session_scope
from sales0.db.migrate_check import head_revision
from sales0.services import data_export
from sales0.services import data_import as plan
from sales0.services.team.passwords import verify_password
from sales0.worker import pull_data
from sales0.worker.client import WorkerApiError, WorkerAuthError, WorkerClient
from sales0.worker.pull_data import ADMIN_EMAIL, PullError, pull
from tests.integration.worker.conftest import BASE, TOKEN

T = Base.metadata.tables
WHEN = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
SECRET_TABLES = ("member_sessions", "password_tokens", "gmail_connections", "workers", "sheet_sources", "sheet_sync_state")


def seed_hosted() -> None:
    """Connected rows incl. the company model, FK cycles, JSON/date columns, and secrets that must never leave."""
    with session_scope() as db:
        db.execute(insert(T["team_members"]), [
            {"id": 7, "name": "Real Person", "email": "real.person@example.test", "role": "admin", "active": True, "created_at": WHEN, "password_hash": "$argon2id$HOSTED-SECRET", "must_reset": False},
            {"id": 8, "name": "Other", "email": "other@example.test", "role": "member", "active": True, "created_at": WHEN, "password_hash": None, "must_reset": False},
        ])
        db.execute(insert(T["member_sessions"]).values(member_id=7, sid_hash="a" * 64, remember=False, created_at=WHEN, rotated_at=WHEN, expires_at=WHEN))
        db.execute(insert(T["linkedin_profiles"]).values(id=50, slug="in/asha", kind="person", profile_url="https://www.linkedin.com/in/asha"))
        db.execute(insert(T["scrape_runs"]).values(id=60, mode="live", status="succeeded", actor_user_id=7))
        db.execute(insert(T["keywords"]).values(id=61, text="ai training", text_key="ai training", actor_user_id=8))
        db.execute(insert(T["seen_posts"]).values(
            id=70, fingerprint="fp1", content_hash="h1", keyword_text="ai training", profile_id=50, first_run_id=60,
            raw_text="raw post text " * 30, ai_result={"verdict": "genuine", "n": [1, 2]}, first_seen_at=WHEN, last_seen_at=WHEN,
            posted_date=WHEN.date(), truncated=True, link_kind="post", raw_card={"inner_text": "card", "reactions": 5},
        ))
        db.execute(insert(T["enquiries"]).values(id=80, content_hash="h1", raw_text="raw", seen_post_id=70, author_profile_id=50, actor_user_id=7))
        db.execute(T["seen_posts"].update().where(T["seen_posts"].c.id == 70).values(enquiry_id=80))  # cycle
        db.execute(insert(T["contacts"]).values(id=90))
        db.execute(insert(T["contacts"]).values(id=91, merged_into_id=90))
        db.execute(insert(T["companies"]).values(id=100, name="Acme Ltd", name_key="acme", domain_key="acme.test", stage="lead", sub_status="new", stage_source="app", stage_changed_at=WHEN))
        db.execute(insert(T["prospects"]).values(id=110, name="Acme", company_id=100, contact_id=90, origin_enquiry_id=80, linkedin_profile_id=50))
        db.execute(insert(T["decks"]).values(id=120, name="Intro", filename="intro.pdf", stored_path="decks/x.pdf", sha256="f" * 64, size_bytes=10, created_at=WHEN, storage="gcs", content_missing=False))
        db.execute(insert(T["llm_calls"]).values(id=95, feature="triage", model="claude-haiku-5-5", status="ok", seen_post_id=70, run_id=60))
        db.execute(insert(T["status_history"]).values(id=96, entity_type="enquiry", entity_id=80, field="status", actor_user_id=7))


@pytest.fixture
def seeded(hosted: TestClient) -> TestClient:
    seed_hosted()
    return hosted


def local_rows(path: Path, sql: str) -> list[tuple]:
    engine = build_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as c:
            return [tuple(r) for r in c.execute(text(sql))]
    finally:
        engine.dispose()


def test_pull_copies_everything_but_secrets(seeded: TestClient, worker_client: WorkerClient, tmp_path: Path) -> None:
    out = tmp_path / "pulled" / "sales0.db"
    lines: list[str] = []
    result = pull(worker_client, out, out=lines.append)
    assert result.ok and out.is_file()
    text_out = "\n".join(lines)
    for private in ("Real Person", "real.person@example", "Acme", "raw post text", "HOSTED-SECRET"):
        assert private not in text_out  # progress shows counts only

    # per-table counts match the server for every exported table
    with session_scope() as db:
        server = {t.name: db.execute(select(func.count()).select_from(t)).scalar_one() for t in plan.import_order()}
    for name, n in server.items():
        assert local_rows(out, f'SELECT count(*) FROM "{name}"')[0][0] == n, name
    assert server["seen_posts"] == 1 and server["companies"] == 1 and server["prospects"] == 1

    # a few row equalities (typed round trip, JSON, cycles, company model)
    assert local_rows(out, "SELECT fingerprint, truncated, link_kind, raw_text FROM seen_posts WHERE id=70") == [("fp1", 1, "post", "raw post text " * 30)]
    engine = build_engine(f"sqlite:///{out}")
    with engine.connect() as c:
        post = c.execute(select(T["seen_posts"]).where(T["seen_posts"].c.id == 70)).mappings().one()
        assert post["first_seen_at"] == WHEN and post["posted_date"] == WHEN.date()
        assert post["ai_result"] == {"verdict": "genuine", "n": [1, 2]} and post["raw_card"] == {"inner_text": "card", "reactions": 5}
        assert post["enquiry_id"] == 80
        assert c.execute(select(T["contacts"].c.merged_into_id).where(T["contacts"].c.id == 91)).scalar_one() == 90
        company = c.execute(select(T["companies"]).where(T["companies"].c.id == 100)).mappings().one()
        assert (company["name"], company["stage"], company["sub_status"]) == ("Acme Ltd", "lead", "new")
        assert c.execute(select(T["prospects"].c.company_id).where(T["prospects"].c.id == 110)).scalar_one() == 100
        deck = c.execute(select(T["decks"]).where(T["decks"].c.id == 120)).mappings().one()
        assert (deck["filename"], deck["size_bytes"]) == ("intro.pdf", 10)  # metadata only; the file stays in GCS
    engine.dispose()

    # secrets and accounts: nothing came across, ONE local admin exists, actor references point at it
    for table in SECRET_TABLES:
        assert local_rows(out, f'SELECT count(*) FROM "{table}"')[0][0] == 0, table
    members = local_rows(out, "SELECT id, email, role, active, password_hash FROM team_members")
    assert [(m[0], m[1], m[2], m[3]) for m in members] == [(1, ADMIN_EMAIL, "admin", 1)]
    assert verify_password(members[0][4], result.admin_password or "") and "HOSTED-SECRET" not in str(members)
    assert local_rows(out, "SELECT actor_user_id FROM keywords WHERE id=61") == [(1,)]
    assert local_rows(out, "SELECT actor_user_id FROM scrape_runs WHERE id=60") == [(1,)]
    assert result.fk_violations == 0
    assert local_rows(out, "SELECT version_num FROM alembic_version") == [(head_revision(),)]


def test_run_script_is_safe_and_ready(seeded: TestClient, worker_client: WorkerClient, tmp_path: Path) -> None:
    out = tmp_path / "pulled" / "sales0.db"
    result = pull(worker_client, out, out=lambda _l: None)
    assert result.run_script is not None and result.run_script.name == "run.sh"
    body = result.run_script.read_text()
    assert result.run_script.stat().st_mode & 0o100
    for needle in (
        "SALES0_MODE=local", f"sqlite:///{out.resolve()}", "SALES0_SCHEDULER_ENABLED=false", "SALES0_REPLY_POLL_MINUTES=0",
        "SALES0_SHEET_SYNC_MINUTES=0", "SALES0_LINKEDIN_MODE=fixture", "SALES0_GMAIL_DAILY_SEND_CAP=0", "SALES0_PORT=8780",
        "python -m sales0",
    ):
        assert needle in body
    assert "HOSTED-SECRET" not in body and TOKEN not in body


def test_the_pulled_file_boots_the_app_without_outbound_ability(seeded: TestClient, worker_client: WorkerClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sales0.config import reset_settings
    from sales0.db.engine import reset_engine
    from sales0.web.app import create_app

    out = tmp_path / "pulled" / "sales0.db"
    result = pull(worker_client, out, out=lambda _l: None)
    for key, value in {
        "SALES0_MODE": "local", "SALES0_WORKER_TOKEN": "", "SALES0_PUBLIC_BASE_URL": "", "SALES0_DATABASE_URL": f"sqlite:///{out}",
        "SALES0_DATA_DIR": str(out.parent), "SALES0_SCHEDULER_ENABLED": "false", "SALES0_LINKEDIN_MODE": "fixture", "SALES0_GMAIL_DAILY_SEND_CAP": "0",
        "SALES0_REPLY_POLL_MINUTES": "0", "SALES0_SHEET_SYNC_MINUTES": "0",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SALES0_WORKER_TOKEN")
    monkeypatch.delenv("SALES0_PUBLIC_BASE_URL")
    reset_settings()
    reset_engine()
    with TestClient(create_app(), base_url="http://127.0.0.1:8780") as app:
        login = app.post("/login", data={"email": ADMIN_EMAIL, "password": result.admin_password}, follow_redirects=False)
        assert login.status_code in (302, 303), login.text[:200]
    # no Gmail connection and no LinkedIn session or cookie exist in the copy
    assert local_rows(out, "SELECT count(*) FROM gmail_connections") == [(0,)]
    assert local_rows(out, "SELECT status FROM linkedin_session") == [("not_connected",)]  # migration default, no cookies/profile
    assert {r[0] for r in local_rows(out, "SELECT status FROM linkedin_accounts")} <= {"not_connected"}
    assert not (out.parent / "google_token.json").exists()
    reset_engine()


def test_refuses_to_overwrite_without_force_and_replaces_with_it(seeded: TestClient, worker_client: WorkerClient, tmp_path: Path) -> None:
    out = tmp_path / "s.db"
    out.write_text("precious")
    with pytest.raises(PullError, match="--force"):
        pull(worker_client, out, out=lambda _l: None)
    assert out.read_text() == "precious"
    assert pull(worker_client, out, force=True, out=lambda _l: None).ok
    assert local_rows(out, "SELECT count(*) FROM keywords") == [(1,)]
    assert not list(tmp_path.glob("*.partial*"))


def test_table_subset(seeded: TestClient, worker_client: WorkerClient, tmp_path: Path) -> None:
    out = tmp_path / "s.db"
    assert pull(worker_client, out, tables=["keywords"], out=lambda _l: None).ok
    assert local_rows(out, "SELECT count(*) FROM keywords") == [(1,)]
    assert local_rows(out, "SELECT count(*) FROM seen_posts") == [(0,)]
    with pytest.raises(PullError, match="not exportable"):
        pull(worker_client, tmp_path / "t.db", tables=["team_members"], out=lambda _l: None)


def test_head_mismatch_is_refused_before_anything_is_written(seeded: TestClient, worker_client: WorkerClient, tmp_path: Path) -> None:
    with session_scope() as db:
        db.execute(text("UPDATE alembic_version SET version_num = 'p9_originuq_b7f2d9a1'"))
    out = tmp_path / "s.db"
    with pytest.raises(PullError, match="schema mismatch"):
        pull(worker_client, out, out=lambda _l: None)
    assert not out.exists() and not list(tmp_path.glob("*.partial*"))


def test_manifest_lists_fk_safe_tables_and_never_the_secret_ones(seeded: TestClient, worker_client: WorkerClient) -> None:
    manifest = worker_client.export_manifest()
    names = [t.name for t in manifest.tables]
    assert names.index("companies") < names.index("prospects") and names.index("linkedin_profiles") < names.index("seen_posts")
    assert not set(names) & set(plan.SKIP) and set(manifest.excluded) == set(plan.SKIP)
    assert manifest.head == head_revision()
    assert {t.name: t.rows for t in manifest.tables}["seen_posts"] == 1


def test_keyset_pagination_visits_every_row_once(seeded: TestClient, worker_client: WorkerClient) -> None:
    with session_scope() as db:
        db.execute(insert(T["keywords"]), [{"id": 200 + i, "text": f"k{i}", "text_key": f"k{i}"} for i in range(25)])
    seen: list[int] = []
    after = None
    pages = 0
    while True:
        page = worker_client.export_batch("keywords", after, 10)
        seen += [r["id"] for r in page.rows]
        pages += 1
        if page.next is None:
            break
        after = page.next
    assert seen == sorted(seen) and len(seen) == len(set(seen)) == 26 and pages == 3
    composite = worker_client.export_batch("contact_tags", None, 5)
    assert composite.rows == [] and composite.next is None


def test_byte_cap_splits_pages_and_trims_only_an_oversized_raw_row(seeded: TestClient) -> None:
    with session_scope() as db:
        db.execute(insert(T["seen_posts"]), [
            {"id": 300 + i, "fingerprint": f"f{i}", "content_hash": f"c{i}", "keyword_text": "k", "raw_text": "x" * 3000, "first_seen_at": WHEN, "last_seen_at": WHEN}
            for i in range(6)
        ])
        db.execute(insert(T["seen_posts"]).values(id=400, fingerprint="big", content_hash="big", keyword_text="k", raw_text="y" * 20000, raw_card={"a": "z" * 20000}, first_seen_at=WHEN, last_seen_at=WHEN))
        first = data_export.batch(db, "seen_posts", None, 100, max_bytes=8000)
        assert 1 <= len(first["rows"]) < 6 and first["next"] is not None  # split by size, not by row limit
        got = list(first["rows"])
        while first["next"] is not None:
            first = data_export.batch(db, "seen_posts", first["next"], 100, max_bytes=8000)
            got += first["rows"]
        ids = [r["id"] for r in got]
        assert ids == sorted(ids) and len(ids) == len(set(ids)) == 8  # seeded 70 + 6 small + the big one
        big = next(r for r in got if r["id"] == 400)
        assert big["raw_text"] is None and big["raw_card"] is None and big["fingerprint"] == "big"
        small = next(r for r in got if r["id"] == 300)
        assert small["raw_text"] == "x" * 3000  # under the cap: kept


def test_export_requires_the_token_and_hosted_mode(seeded: TestClient) -> None:
    for path in ("/api/worker/export/manifest", "/api/worker/export/batch?table=keywords"):
        assert seeded.get(path).status_code == 401
        assert seeded.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert seeded.get(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    for table in ("team_members", "member_sessions", "gmail_connections", "linkedin_accounts", "app_meta", "nope"):
        r = seeded.get(f"/api/worker/export/batch?table={table}", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "table_not_allowed", table
    bad = seeded.get("/api/worker/export/batch?table=keywords&after=%5B1,2%5D", headers={"Authorization": f"Bearer {TOKEN}"})
    assert bad.status_code == 422
    assert seeded.get("/api/worker/export/batch?table=keywords&limit=99999", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 422
    with pytest.raises(WorkerAuthError):
        WorkerClient(BASE, "wrong", http=seeded, sleep=lambda _s: None).export_manifest()


def test_local_mode_has_no_export_endpoint(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from sales0.config import reset_settings
    from sales0.web.app import create_app

    monkeypatch.setenv("SALES0_WORKER_TOKEN", "t" * 40)
    reset_settings()
    with TestClient(create_app(), base_url=BASE) as client:
        for path in ("/api/worker/export/manifest", "/api/worker/export/batch?table=keywords"):
            assert client.get(path, headers={"Authorization": f"Bearer {'t' * 40}"}).status_code == 404
        assert client.get("/api/worker/export/manifest").status_code == 401  # token still checked first


def test_a_failed_pull_leaves_no_file(seeded: TestClient, worker_client: WorkerClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise WorkerApiError(500, "boom", "x")

    monkeypatch.setattr(WorkerClient, "export_batch", boom)
    out = tmp_path / "s.db"
    with pytest.raises(WorkerApiError):
        pull(worker_client, out, out=lambda _l: None)
    assert not out.exists() and not list(tmp_path.glob("*.partial*"))
    assert pull_data.PORT == 8780
