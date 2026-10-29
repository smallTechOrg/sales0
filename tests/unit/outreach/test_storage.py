"""Attachment store: backend choice (local / gcs / refused when hosted without a bucket), key safety, round trips.
GCS is a fake client: no Google call."""

from __future__ import annotations

from pathlib import Path

import pytest

from sales0.config import reset_settings
from sales0.errors import DomainError
from sales0.services.outreach import storage
from tests.support.fake_gcs import FakeGcsClient


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):  # noqa: ANN202
    monkeypatch.setenv("SALES0_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SALES0_ATTACHMENTS_BUCKET", raising=False)
    monkeypatch.delenv("SALES0_MODE", raising=False)
    reset_settings()
    yield
    storage.reset_storage()
    reset_settings()


def test_backend_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    assert storage.current_backend() == "local"
    monkeypatch.setenv("SALES0_ATTACHMENTS_BUCKET", "b1")
    reset_settings()
    assert storage.current_backend() == "gcs"
    monkeypatch.delenv("SALES0_ATTACHMENTS_BUCKET")
    monkeypatch.setenv("SALES0_MODE", "hosted")
    monkeypatch.setenv("SALES0_ATTACHMENTS_BUCKET", "  ")  # blank counts as unset
    reset_settings()
    for call in (storage.current_backend, lambda: storage.store_for("local"), lambda: storage.store_for("gcs")):
        with pytest.raises(DomainError) as exc:
            call()
        assert exc.value.code == "attachments_not_configured" and exc.value.http_status == 409


def test_local_round_trip_and_key_safety(tmp_path: Path) -> None:
    store = storage.current_store()
    key = storage.deck_key("ab" * 32)
    assert key == f"decks/{'ab' * 32}.pdf" and store.get(key) is None and not store.exists(key)
    store.put(key, b"%PDF-x")
    assert store.get(key) == b"%PDF-x" and (tmp_path / key).is_file()
    store.delete(key)
    store.delete(key)  # idempotent
    for bad in ("../x.pdf", "/etc/passwd", "a\\b"):
        with pytest.raises(DomainError):
            store.get(bad)


def test_gcs_round_trip_with_a_fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_ATTACHMENTS_BUCKET", "bucket-y")
    reset_settings()
    client = FakeGcsClient()
    storage.set_gcs_client_factory(lambda: client)
    store = storage.current_store()
    assert store.name == "gcs" and store.get("decks/x.pdf") is None
    store.put("decks/x.pdf", b"%PDF-y")
    assert store.exists("decks/x.pdf") and store.get("decks/x.pdf") == b"%PDF-y"
    store.delete("decks/x.pdf")
    store.delete("decks/x.pdf")
    assert client.buckets["bucket-y"].objects == {}
