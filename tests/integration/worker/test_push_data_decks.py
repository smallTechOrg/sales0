"""``sales0 push-data`` carries deck FILES: after the rows, each local ``<dir>/decks/<sha>.pdf`` goes to the hosted app
in ordered base64 pieces (each request under the 8 MB cap) and the hosted app writes it to its GCS bucket (a fake GCS
client here: no Google call). A deck with no local file stays "file missing"; a re-run skips files already there and
never resets the server-owned storage columns."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import insert

from sales0.config import reset_settings
from sales0.db.base import Base
from sales0.db.engine import build_engine, session_scope
from sales0.db.migrate_check import alembic_config
from sales0.db.models import Deck
from sales0.services import data_import
from sales0.services.outreach import storage
from sales0.worker import protocol as p
from sales0.worker.client import WorkerApiError, WorkerClient
from sales0.worker.push_data import push
from tests.support.fake_gcs import FakeGcsClient

T = Base.metadata.tables
BUCKET = "sales0-attachments-test"
BIG = b"%PDF-1.4\n" + bytes(range(256)) * 40 + b"\n%%EOF\n"  # ~10 KB: several pieces at the patched chunk size
SMALL = b"%PDF-1.4\nsmall deck\n%%EOF\n"
GONE = b"%PDF-1.4\nthis file was lost\n%%EOF\n"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def gcs(monkeypatch: pytest.MonkeyPatch, hosted_env: None) -> FakeGcsClient:
    monkeypatch.setenv("SALES0_ATTACHMENTS_BUCKET", BUCKET)
    reset_settings()
    client = FakeGcsClient()
    storage.set_gcs_client_factory(lambda: client)
    return client


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "local.db"
    url = f"sqlite:///{path}"
    command.upgrade(alembic_config(url), "head")
    engine = build_engine(url)
    with engine.begin() as c:
        c.execute(insert(T["decks"]), [
            {"id": i, "name": n, "filename": f"{n}.pdf", "stored_path": f"decks/{sha(b)}.pdf", "sha256": sha(b),
             "size_bytes": len(b), "storage": "local", "content_missing": n == "Gone"}
            for i, n, b in ((1, "Big", BIG), (2, "Small", SMALL), (3, "Gone", GONE))
        ])  # fmt: skip
    engine.dispose()
    (tmp_path / "decks").mkdir()
    for b in (BIG, SMALL):
        (tmp_path / "decks" / f"{sha(b)}.pdf").write_bytes(b)  # the local data dir next to the database
    return path


def test_deck_files_go_to_the_hosted_bucket_in_pieces_and_rerun_skips(
    gcs: FakeGcsClient, source: Path, hosted: TestClient, worker_client: WorkerClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(data_import, "DECK_CHUNK_BYTES", 4096)
    calls: list[p.ImportDeckChunkRequest] = []
    real = worker_client.import_deck_chunk

    def spy(req: p.ImportDeckChunkRequest) -> p.ImportDeckChunkResponse:
        calls.append(req)
        return real(req)

    monkeypatch.setattr(worker_client, "import_deck_chunk", spy)
    lines: list[str] = []
    assert push(source, worker_client, out=lines.append) is True
    assert [c.deck_id for c in calls] == [1, 1, 1, 2]  # one deck at a time; 10 KB in 4 KB pieces
    assert all(len(c.data_b64) < 8 * 1024 * 1024 for c in calls)
    assert any("'Gone.pdf' has no file locally" in line for line in lines)
    objects = gcs.buckets[BUCKET].objects
    assert objects == {f"decks/{sha(BIG)}.pdf": BIG, f"decks/{sha(SMALL)}.pdf": SMALL}  # pieces cleaned up
    with session_scope() as db:
        big, small, gone = db.get(Deck, 1), db.get(Deck, 2), db.get(Deck, 3)
        assert (big.storage, big.content_missing) == ("gcs", False) and (small.storage, small.content_missing) == ("gcs", False)
        assert gone.content_missing is True
        from sales0.services.outreach import decks

        assert decks.read_deck_bytes(big) == BIG

    calls.clear()
    lines.clear()
    assert push(source, worker_client, out=lines.append) is True
    assert [c.deck_id for c in calls] == [1, 2] and any("'Big.pdf' (10 KB) already there" in line for line in lines)
    with session_scope() as db:  # the row upsert did not reset the server-owned columns
        assert (db.get(Deck, 1).storage, db.get(Deck, 1).content_missing) == ("gcs", False)


def test_pieces_are_checked(gcs: FakeGcsClient, source: Path, hosted: TestClient, worker_client: WorkerClient) -> None:
    assert push(source, worker_client, out=lambda _l: None) is True

    def req(**kw: object) -> p.ImportDeckChunkRequest:
        base = {"deck_id": 3, "sha256": sha(GONE), "offset": 0, "total": len(GONE), "data_b64": base64.b64encode(GONE).decode()}
        return p.ImportDeckChunkRequest(**{**base, **kw})

    with pytest.raises(WorkerApiError) as exc:
        worker_client.import_deck_chunk(req(sha256=sha(BIG)))
    assert exc.value.code == "deck_hash_mismatch"
    with pytest.raises(WorkerApiError) as exc:
        worker_client.import_deck_chunk(req(offset=5, data_b64=base64.b64encode(GONE[5:]).decode()))
    assert exc.value.code == "deck_chunk_order"
    ok = worker_client.import_deck_chunk(req())
    assert ok.complete and not ok.skipped
    with session_scope() as db:
        assert db.get(Deck, 3).content_missing is False and gcs.buckets[BUCKET].objects[f"decks/{sha(GONE)}.pdf"] == GONE


def test_hosted_without_a_bucket_refuses_deck_files(source: Path, hosted: TestClient, worker_client: WorkerClient) -> None:
    lines: list[str] = []
    with pytest.raises(WorkerApiError) as exc:
        push(source, worker_client, out=lines.append)
    assert exc.value.code == "attachments_not_configured"


def test_dry_run_reports_deck_files(source: Path) -> None:
    lines: list[str] = []
    assert push(source, None, dry_run=True, out=lines.append) is True
    assert "Deck files (sent one at a time, in pieces): 2 of 3 found locally" in lines
