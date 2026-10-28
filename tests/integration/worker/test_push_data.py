"""``sales0 push-data`` end to end, in-process: a real migrated SQLite source file -> the hosted app's API -> the hosted
database (SQLite here; PostgreSQL when SALES0_TEST_POSTGRES_URL is set, which also proves the sequence reset)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select

from sales0.db.base import Base
from sales0.db.engine import build_engine, session_scope
from sales0.db.migrate_check import alembic_config
from sales0.db.models import Keyword, LinkedinProfile, SeenPost, TeamMember
from sales0.worker import protocol as p
from sales0.worker.client import WorkerApiError, WorkerClient
from sales0.worker.push_data import push
from tests.integration.worker.conftest import BASE, TOKEN

T = Base.metadata.tables
WHEN = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)


def build_source(path: Path) -> None:
    """A migrated local database with one connected set of rows, including both FK cycles and a secret to withhold."""
    url = f"sqlite:///{path}"
    command.upgrade(alembic_config(url), "head")
    engine = build_engine(url)
    with engine.begin() as c:
        c.execute(insert(T["team_members"]), [
            {"id": 7, "name": "Ops", "email": "Ops@Example.test", "password_hash": "$argon2id$SECRET"},
            {"id": 8, "name": "Gone", "email": "gone@example.test", "password_hash": None},
        ])
        c.execute(insert(T["linkedin_profiles"]).values(id=50, slug="in/asha", kind="person", profile_url="https://www.linkedin.com/in/asha"))
        c.execute(insert(T["scrape_runs"]).values(id=60, mode="live", status="succeeded", actor_user_id=7, linkedin_account_id=1))
        c.execute(insert(T["keywords"]).values(id=61, text="ai training", text_key="ai training", actor_user_id=8))
        c.execute(insert(T["seen_posts"]).values(
            id=70, fingerprint="fp1", content_hash="h1", keyword_text="ai training", profile_id=50, first_run_id=60,
            raw_text="the full raw post text " * 40, post_url="https://www.linkedin.com/feed/update/urn:li:activity:1",
            ai_result={"verdict": "genuine", "n": [1, 2]}, first_seen_at=WHEN, last_seen_at=WHEN,
        ))
        c.execute(insert(T["enquiries"]).values(id=80, content_hash="h1", raw_text="raw", seen_post_id=70, author_profile_id=50, actor_user_id=7))
        c.execute(T["seen_posts"].update().where(T["seen_posts"].c.id == 70).values(enquiry_id=80))  # the cycle
        c.execute(insert(T["contacts"]).values(id=90))
        c.execute(insert(T["contacts"]).values(id=91, merged_into_id=90))  # self reference
        c.execute(insert(T["llm_calls"]).values(id=95, feature="triage", model="claude-haiku-5-5", status="ok", seen_post_id=70, run_id=60))
        c.execute(insert(T["status_history"]).values(id=96, entity_type="enquiry", entity_id=80, field="status", actor_user_id=7))
    engine.dispose()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "local.db"
    build_source(path)
    return path


def hosted_counts() -> dict[str, int]:
    with session_scope() as db:
        return {n: db.execute(select(func.count()).select_from(T[n])).scalar_one() for n in ("seen_posts", "enquiries", "contacts", "llm_calls", "keywords")}


def test_pushes_everything_in_fk_order_and_is_idempotent(source: Path, hosted: TestClient, worker_client: WorkerClient) -> None:
    before = source.read_bytes()
    with session_scope() as db:
        db.add(TeamMember(name="Ops H", email="ops@example.test", role="member"))
        db.flush()
        hosted_ops = db.execute(select(TeamMember.id).where(TeamMember.email == "ops@example.test")).scalar_one()
    lines: list[str] = []

    assert push(source, worker_client, out=lines.append) is True
    assert "All tables match." in lines

    with session_scope() as db:
        post = db.get(SeenPost, 70)
        assert post is not None
        assert post.raw_text == "the full raw post text " * 40  # the raw capture survives the trip
        assert post.ai_result == {"verdict": "genuine", "n": [1, 2]}
        assert post.first_seen_at == WHEN  # timezone-aware datetime round trip
        assert post.enquiry_id == 80  # the cyclic link was filled in by the second pass
        assert db.execute(select(T["contacts"].c.merged_into_id).where(T["contacts"].c.id == 91)).scalar_one() == 90
        assert db.get(LinkedinProfile, 50) is not None
        # actor references re-map by e-mail (case-insensitive); an unknown member becomes NULL
        assert db.get(Keyword, 61).actor_user_id is None
        assert db.execute(select(T["scrape_runs"].c.actor_user_id).where(T["scrape_runs"].c.id == 60)).scalar_one() == hosted_ops
        # nothing secret or machine-local came across
        assert db.execute(select(func.count()).select_from(T["team_members"]).where(T["team_members"].c.email == "gone@example.test")).scalar_one() == 0
        assert db.execute(select(T["team_members"].c.password_hash).where(T["team_members"].c.email == "ops@example.test")).scalar_one() is None

    first = hosted_counts()
    assert push(source, worker_client, out=lambda _l: None) is True  # second run: an upsert, no duplicates
    assert hosted_counts() == first
    assert source.read_bytes() == before  # the source file is never written

    with session_scope() as db:  # new rows after the import get fresh ids (PostgreSQL sequences were moved past MAX(id))
        kw = Keyword(text="after import", text_key="after import")
        db.add(kw)
        db.flush()
        assert kw.id > 61


def test_dry_run_sends_nothing(source: Path) -> None:
    lines: list[str] = []
    assert push(source, None, dry_run=True, out=lines.append) is True
    assert any("seen_posts" in line for line in lines)
    assert "Dry run: nothing was sent." in lines


def test_a_source_on_an_older_schema_is_upgraded_on_its_copy(tmp_path: Path, hosted: TestClient, worker_client: WorkerClient) -> None:
    path = tmp_path / "old.db"
    build_source(path)
    command.downgrade(alembic_config(f"sqlite:///{path}"), "-1")
    assert push(path, worker_client, out=lambda _l: None) is True


def test_rejects_tables_that_must_never_be_imported(hosted: TestClient, worker_client: WorkerClient) -> None:
    for table in ("team_members", "gmail_connections", "linkedin_session", "alembic_version", "nope"):
        with pytest.raises(WorkerApiError) as err:
            worker_client.import_batch(p.ImportBatchRequest(table=table, rows=[{"id": 1}]))
        assert err.value.status == 422 and err.value.code == "table_not_allowed"


def test_requires_the_worker_token_and_hosted_mode(hosted: TestClient) -> None:
    body = {"table": "keywords", "rows": []}
    assert hosted.post("/api/worker/import/batch", json=body).status_code == 401
    assert hosted.post("/api/worker/import/batch", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = hosted.post("/api/worker/import/batch", json=body, headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200


def test_a_bad_row_is_a_clean_422_not_a_crash(hosted: TestClient, worker_client: WorkerClient) -> None:
    with pytest.raises(WorkerApiError) as err:
        worker_client.import_batch(p.ImportBatchRequest(table="keywords", rows=[{"id": 1, "bogus": 1}]))
    assert err.value.code == "bad_row"
    with pytest.raises(WorkerApiError) as err:  # FK to a row that was never sent
        worker_client.import_batch(p.ImportBatchRequest(table="seen_posts", rows=[
            {"id": 1, "fingerprint": "x", "content_hash": "x", "keyword_text": "k", "profile_id": 999},
        ]))
    assert err.value.code == "import_conflict"
    assert hosted_counts()["seen_posts"] == 0  # the failed batch rolled back


def test_local_mode_has_no_import_endpoint(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from sales0.config import reset_settings
    from sales0.web.app import create_app

    monkeypatch.setenv("SALES0_WORKER_TOKEN", "t" * 40)
    reset_settings()
    with TestClient(create_app(), base_url=BASE) as client:
        r = client.post("/api/worker/import/batch", json={"table": "keywords", "rows": []}, headers={"Authorization": f"Bearer {'t' * 40}"})
        assert r.status_code == 404



def test_imported_raw_text_follows_the_hosted_retention(source: Path, hosted: TestClient, worker_client: WorkerClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from sales0.config import reset_settings

    monkeypatch.setenv("SALES0_SEEN_RAW_RETENTION_DAYS", "36500")
    reset_settings()
    engine = build_engine(f"sqlite:///{source}")
    with engine.begin() as c:
        c.execute(T["seen_posts"].update().where(T["seen_posts"].c.id == 70).values(raw_purge_after=WHEN))
    engine.dispose()
    assert push(source, worker_client, out=lambda _l: None) is True
    with session_scope() as db:
        assert db.get(SeenPost, 70).raw_purge_after.year > 2100
