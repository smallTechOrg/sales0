"""Pure pieces of ``sales0 pull-data`` / ``/api/worker/export/*``: JSON encoding, cursors, the skip list, the head check."""

from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sales0.db.base import Base
from sales0.db.migrate_check import head_revision
from sales0.errors import DomainError
from sales0.services import data_export as ex
from sales0.services import data_import as plan
from sales0.worker import pull_data as pd

NEVER = {
    "team_members", "member_sessions", "password_tokens", "gmail_connections", "linkedin_accounts", "linkedin_session",
    "workers", "app_meta", "sheet_sources", "sheet_sync_state",
}


def test_encoding_is_json_safe_for_every_column_kind() -> None:
    row = {
        "when": datetime(2026, 10, 1, 9, 30, tzinfo=UTC), "day": date(2026, 10, 1), "money": Decimal("1.50"), "blob": b"\x00\xffab",
        "json": {"a": [1, {"d": date(2026, 1, 2)}], "n": None}, "flag": True, "f": 1.5, "s": "x", "none": None,
    }
    encoded = ex.encode_row(row)
    json.dumps(encoded)  # must not raise
    assert encoded["when"] == "2026-10-01T09:30:00+00:00" and encoded["day"] == "2026-10-01" and encoded["money"] == "1.50"
    assert base64.b64decode(encoded["blob"]) == b"\x00\xffab" and encoded["json"]["a"][1]["d"] == "2026-01-02"
    assert encoded["flag"] is True and encoded["none"] is None


def test_encoded_dates_parse_back_by_column_type() -> None:
    col = Base.metadata.tables["seen_posts"].c.first_seen_at
    assert plan._coerce_value(col, ex.encode_value(datetime(2026, 10, 1, 9, 30, tzinfo=UTC))) == datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
    day = Base.metadata.tables["seen_posts"].c.posted_date
    assert plan._coerce_value(day, "2026-10-01") == date(2026, 10, 1)


def test_cursor_round_trip_and_validation() -> None:
    single, composite = Base.metadata.tables["keywords"], Base.metadata.tables["contact_tags"]
    assert ex.decode_cursor(None, single) is None and ex.decode_cursor("", single) is None
    assert ex.decode_cursor(ex.encode_cursor([42]), single) == [42]
    assert ex.decode_cursor("42", single) == [42]
    assert ex.decode_cursor("[3,4]", composite) == [3, 4]
    for bad in ("[1,2]", "not json", "[true]", "{}", "[]"):
        with pytest.raises(DomainError) as err:
            ex.decode_cursor(bad, single)
        assert err.value.code == "bad_cursor"
    with pytest.raises(DomainError):
        ex.decode_cursor("[1]", composite)


def test_exported_tables_are_exactly_the_importable_ones_and_never_secrets() -> None:
    names = {t.name for t in plan.import_order()}
    assert not names & NEVER and not names & set(plan.SKIP)
    assert NEVER <= set(plan.SKIP)
    for secret in NEVER:
        with pytest.raises(DomainError) as err:
            ex.exportable_table(secret)
        assert err.value.code == "table_not_allowed"
    assert {"companies", "prospects", "seen_posts", "decks", "contacts"} <= names


def test_trimmable_columns_are_only_raw_capture() -> None:
    assert ex.TRIMMABLE_COLUMNS == ("raw_card", "raw_text")


def test_head_mismatch_is_refused_with_a_direction() -> None:
    pd.check_head(head_revision(), head_revision())
    with pytest.raises(pd.PullError, match="schema mismatch"):
        pd.check_head("0001_baseline", head_revision())  # server older: the server is behind
    with pytest.raises(pd.PullError, match="server is at none"):
        pd.check_head(None, head_revision())
    with pytest.raises(pd.PullError, match="Update the repo"):
        pd.check_head("zz_from_the_future", head_revision())


def test_member_and_account_references_are_repointed() -> None:
    table = Base.metadata.tables["keywords"]
    rows = pd.prepare_rows(table, [{"id": 1, "text": "k", "text_key": "k", "actor_user_id": 77}], {1})
    assert rows[0]["actor_user_id"] == pd.ADMIN_ID and rows[0]["text"] == "k"
    with pytest.raises(pd.PullError, match="column"):
        pd.prepare_rows(table, [{"id": 1, "from_the_future": 1}], {1})
    runs = Base.metadata.tables["scrape_runs"]
    mapped = pd.prepare_rows(runs, [{"id": 1, "mode": "live", "status": "succeeded", "linkedin_account_id": 9}], {1})
    assert mapped[0]["linkedin_account_id"] in (None, 1)  # an account this machine does not have is not kept


def test_run_script_contents(tmp_path: Path) -> None:
    db = tmp_path / "my data" / "s.db"
    db.parent.mkdir()
    script = pd.write_run_script(db)
    body = script.read_text()
    assert "'sqlite:///" + str(db) + "'" in body  # quoted: a path with a space survives
    assert body.startswith("#!/usr/bin/env bash") and script.stat().st_mode & 0o100
