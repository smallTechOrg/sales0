"""Migration p9_lirekey_7c3e9a15: person sheet keys follow the one LinkedIn canonicaliser; ``flags.kind`` gains
``source_row_removed``. Seeded DB, no data loss, collisions keep their identity, downgrade restores."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import text

from sales0 import linkedin_url as li
from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config

PARENT = "p9_originuq_b7f2d9a1"
REV = "p9_lirekey_7c3e9a15"
MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "versions" / "p9_lirekey_7c3e9a15.py"
N = "2026-10-20 10:00:00.000000"

URLS = [
    "https://www.linkedin.com/in/Pat-Example-123/?trk=x", "linkedin.com/in/pat", "//uk.linkedin.com/in/pat", "https://www.linkedin.com/in/%E0%A4%85-1/",
    "https://www.linkedin.com/in/pri%2Dya", "https://www.linkedin.com/in/Café", "https://www.linkedin.com/company/Acme-Corp/about/",
    "https://www.linkedin.com/school/Uni", "https://www.linkedin.com/pub/jane-doe/1/2/3", "https://lnkd.in/p/abc", "https://evil.example/?u=linkedin.com/in/x",
    "https://linkedin.com.evil.example/in/x", "https://www.linkedin.com/feed/update/urn:li:activity:1/", "ftp://www.linkedin.com/in/x", "/in/rel", "", None,
]


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("p9_lirekey", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_frozen_canonicaliser_equals_the_app_version() -> None:
    m = _load()
    for url in URLS:
        assert m.canonical_linkedin(url) == li.canonical_linkedin(url), url
        assert m.key_of(url) == li.key_of(url), url


def _src(c, key: str, tab: str, fields: dict, row_hash: str, target: int) -> None:  # noqa: ANN001
    c.execute(
        text(
            "INSERT INTO sheet_sources (entity, key, target_type, target_id, sheet_id, gid, tab_name, row_number, row_hash, fields, first_seen_at, last_seen_at) "
            "VALUES ('prospect', :k, 'prospect', :t, 'sheet-main', :g, 'T', 2, :h, :f, :n, :n)"
        ),
        {"k": key, "t": target, "g": tab, "h": row_hash, "f": json.dumps(fields), "n": N},
    )


@pytest.fixture
def seeded(tmp_path):  # type: ignore[no-untyped-def]
    url = f"sqlite:///{(tmp_path / 'p9.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, PARENT)
    engine = build_engine(url)
    with engine.begin() as c:
        _src(c, "li:acme-corp", "13", {"name": "Acme page", "company": "Acme", "linkedin_url": "linkedin.com/company/Acme-Corp"}, "h1", 1)  # company page filed as a person
        _src(c, "li:pat-example-123", "13", {"name": "Pat", "linkedin_url": "https://www.linkedin.com/in/Pat-Example-123/"}, "h2", 2)  # unchanged
        _src(c, "li:pri%2dya", "13", {"name": "Pri", "linkedin_url": "https://www.linkedin.com/in/pri%2Dya"}, "h3", 3)  # percent-encoded slug
        _src(c, "li:pri-ya", "13", {"name": "Pri again", "linkedin_url": "https://www.linkedin.com/in/pri-ya"}, "h4", 4)  # collides after decoding
        _src(c, "li:x", "13", {"name": "Evil", "company": "E", "linkedin_url": "https://evil.example/?u=linkedin.com/in/x"}, "h5", 5)  # not a LinkedIn link
        _src(c, "li:acme-corp", "12", {"name": "Acme page", "linkedin_url": "linkedin.com/company/Acme-Corp"}, "h6", 1)  # same page in a 2nd tab
        c.execute(text("INSERT INTO sheet_sources (entity, key, target_type, target_id, sheet_id, gid, tab_name, row_number, row_hash, fields, first_seen_at, last_seen_at) VALUES ('enquiry','post:1','enquiry',9,'sheet-main','0','T',2,'h7','{}',:n,:n)"), {"n": N})
        c.execute(text("INSERT INTO flags (entity_type, entity_id, kind, source, created_at, updated_at) VALUES ('prospect', 1, 'repeat_poster', 'auto', :n, :n)"), {"n": N})
    engine.dispose()
    return {"url": url, "cfg": cfg}


def _keys(url: str) -> dict[tuple[str, str], str]:
    engine = build_engine(url)
    try:
        with engine.connect() as c:
            return {(r.gid, r.row_hash): r.key for r in c.execute(text("SELECT gid, row_hash, key FROM sheet_sources"))}
    finally:
        engine.dispose()


def test_upgrade_rekeys_without_losing_rows_and_widens_the_flag_check(seeded) -> None:  # type: ignore[no-untyped-def]
    command.upgrade(seeded["cfg"], REV)
    keys = _keys(seeded["url"])
    assert len(keys) == 7  # nothing added, nothing deleted
    assert keys[("13", "h1")] == "li:company/acme-corp" and keys[("12", "h6")] == "li:company/acme-corp"  # kind kept; both tabs agree
    assert keys[("13", "h2")] == "li:pat-example-123"  # untouched
    assert keys[("13", "h4")] == "li:pri-ya"  # already canonical: keeps the key
    assert keys[("13", "h3")] == "row:h3"  # decodes onto a taken key: keeps its own identity, no prospect merged
    assert keys[("13", "h5")].startswith("nc:")  # no longer a LinkedIn profile: name + company
    assert keys[("0", "h7")] == "post:1"
    engine = build_engine(seeded["url"])
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM flags")).scalar_one() == 1
        c.execute(text("INSERT INTO flags (entity_type, entity_id, kind, source, created_at, updated_at) VALUES ('prospect', 2, 'source_row_removed', 'auto', :n, :n)"), {"n": N})
        with pytest.raises(Exception, match="(?i)unique|constraint"):
            c.execute(text("INSERT INTO flags (entity_type, entity_id, kind, source, created_at, updated_at) VALUES ('prospect', 2, 'source_row_removed', 'auto', :n, :n)"), {"n": N})
    with engine.connect() as c:
        with pytest.raises(Exception, match="(?i)check"):
            c.execute(text("INSERT INTO flags (entity_type, entity_id, kind, source, created_at, updated_at) VALUES ('prospect', 3, 'bogus', 'auto', :n, :n)"), {"n": N})
        names = {r[1] for r in c.execute(text("PRAGMA index_list(flags)"))}
        assert {"uq_flags_active", "ix_flags_kind_cleared_at"} <= names
    engine.dispose()


def test_downgrade_restores_old_keys_and_drops_only_the_new_flags(seeded) -> None:  # type: ignore[no-untyped-def]
    command.upgrade(seeded["cfg"], REV)
    engine = build_engine(seeded["url"])
    with engine.begin() as c:
        c.execute(text("INSERT INTO flags (entity_type, entity_id, kind, source, created_at, updated_at) VALUES ('prospect', 2, 'source_row_removed', 'auto', :n, :n)"), {"n": N})
    engine.dispose()
    command.downgrade(seeded["cfg"], PARENT)
    keys = _keys(seeded["url"])
    assert len(keys) == 7 and keys[("13", "h1")] == "li:acme-corp" and keys[("12", "h6")] == "li:acme-corp"
    engine = build_engine(seeded["url"])
    with engine.connect() as c:
        assert [r[0] for r in c.execute(text("SELECT kind FROM flags"))] == ["repeat_poster"]
    engine.dispose()
