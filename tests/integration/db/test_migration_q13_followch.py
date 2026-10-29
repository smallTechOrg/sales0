"""Migration q13_followch_3e7a6c58: ``decks.storage`` (existing rows = local) and ``decks.content_missing`` (TRUE where
the file is no longer on disk, as on the wiped hosted /tmp), ``follow_ups.channel``. Retry-safe; downgrade removes them."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from sales0.config import get_settings, reset_settings
from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, head_revision

PARENT = "p11_rawcapture_a4d9c7e2"
REV = "q13_followch_3e7a6c58"
ON_DISK = b"%PDF-1.4\non disk\n%%EOF\n"
LOST = b"%PDF-1.4\nlost on restart\n%%EOF\n"


def cols(url: str, table: str) -> set[str]:
    engine = build_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def test_flags_missing_deck_files_and_downgrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "decks").mkdir(parents=True)
    monkeypatch.setenv("SALES0_DATA_DIR", str(data_dir))
    reset_settings()
    assert get_settings().data_path == data_dir
    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = alembic_config(url)
    command.upgrade(cfg, PARENT)
    assert "storage" not in cols(url, "decks") and "channel" not in cols(url, "follow_ups")
    good, lost = hashlib.sha256(ON_DISK).hexdigest(), hashlib.sha256(LOST).hexdigest()
    (data_dir / "decks" / f"{good}.pdf").write_bytes(ON_DISK)
    engine = build_engine(url)
    with engine.begin() as c:
        for i, (sha, size) in enumerate(((good, len(ON_DISK)), (lost, len(LOST))), start=1):
            c.execute(
                text("INSERT INTO decks (id, name, filename, stored_path, sha256, size_bytes, created_at, active) "
                     "VALUES (:id, :n, :f, :p, :s, :z, '2026-10-01 00:00:00', 1)"),
                {"id": i, "n": f"Deck {i}", "f": f"d{i}.pdf", "p": f"decks/{sha}.pdf", "s": sha, "z": size},
            )  # fmt: skip
    engine.dispose()

    command.upgrade(cfg, REV)
    engine = build_engine(url)
    with engine.connect() as c:
        rows = c.execute(text("SELECT id, storage, content_missing FROM decks ORDER BY id")).all()
    engine.dispose()
    assert [(r[1], bool(r[2])) for r in rows] == [("local", False), ("local", True)]
    assert "channel" in cols(url, "follow_ups")

    command.upgrade(cfg, REV)  # re-runnable
    command.downgrade(cfg, PARENT)
    assert not {"storage", "content_missing"} & cols(url, "decks") and "channel" not in cols(url, "follow_ups")


def test_revision_chain() -> None:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    rev = script.get_revision(REV)
    assert rev is not None and rev.down_revision == PARENT
    assert REV in {r.revision for r in script.iterate_revisions(head_revision(), "base")}  # an ancestor of (or the) head
