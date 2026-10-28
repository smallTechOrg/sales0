"""Migration p5_replies_c3d81f20 on a real SQLite file: upgrade, constraints, downgrade, data survival."""

from __future__ import annotations

from alembic import command
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, current_revision, head_revision

P3 = "p3_outreach_a7c41e9d"
P5_TABLES = {"outreach_replies", "suppressions", "reply_poll_state"}
P5_COLUMNS = {"reply_status", "last_reply_at", "follow_up_of_id"}
NOW = datetime(2026, 10, 28, tzinfo=UTC).isoformat()


def _insp(url: str):  # type: ignore[no-untyped-def]
    engine = build_engine(url)
    try:
        insp = inspect(engine)
        return set(insp.get_table_names()), {c["name"] for c in insp.get_columns("outreach_messages")}
    finally:
        engine.dispose()


def _seed(url: str) -> tuple[int, int]:
    """One team member + one outreach message; returns (member_id, message_id)."""
    engine = build_engine(url)
    try:
        with engine.begin() as c:
            c.execute(text("INSERT INTO team_members (name, email, created_at) VALUES ('Ann', 'ann@t.example', :n)"), {"n": NOW})
            mid = c.execute(text("SELECT id FROM team_members")).scalar_one()
            c.execute(
                text(
                    "INSERT INTO outreach_messages (member_id, connection_email, from_addr, to_addrs, cc_addrs, subject, body, state, created_at, updated_at) "
                    "VALUES (:m, 'ann@t.example', 'ann@t.example', 'p@acme.example', '', 'S', 'B', 'sent', :n, :n)"
                ),
                {"m": mid, "n": NOW},
            )
            msg = c.execute(text("SELECT id FROM outreach_messages")).scalar_one()
        return int(mid), int(msg)
    finally:
        engine.dispose()


def _reply_sql(member_id: int, message_id: int, *, gmail_id: str = "g1", kind: str = "human", intent: str | None = "other") -> tuple[str, dict[str, object]]:
    return (
        "INSERT INTO outreach_replies (message_id, member_id, gmail_message_id, gmail_thread_id, from_addr, received_at, snippet, kind, intent, classified_by, handled, created_at) "
        "VALUES (:msg, :mem, :gid, 't', 'p@acme.example', :n, '', :kind, :intent, 'llm', 0, :n)",
        {"msg": message_id, "mem": member_id, "gid": gmail_id, "kind": kind, "intent": intent, "n": NOW},
    )


def test_upgrade_creates_the_tables_columns_and_is_head(db_url: str) -> None:
    engine = build_engine(db_url)
    try:
        assert current_revision(engine) == head_revision()
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        assert P5_TABLES <= tables
        cols = {c["name"]: c for c in insp.get_columns("outreach_messages")}
        assert P5_COLUMNS <= set(cols)
        assert cols["reply_status"]["nullable"] is False and "none" in str(cols["reply_status"]["default"])
        assert {c["name"] for c in insp.get_columns("outreach_replies")} >= {
            "message_id", "member_id", "gmail_message_id", "gmail_thread_id", "from_addr", "received_at", "snippet", "kind", "intent",
            "reason", "classified_by", "handled", "handled_by_member_id", "handled_at", "created_at",
        }
        assert {c["name"] for c in insp.get_columns("reply_poll_state")} >= {"member_id", "last_polled_at", "last_ok_at", "last_error", "consecutive_failures", "next_attempt_at", "last_new_replies"}
        assert {fk["referred_table"] for fk in insp.get_foreign_keys("outreach_messages")} >= {"team_members", "outreach_messages"}
    finally:
        engine.dispose()


def test_unique_constraints(db_url: str) -> None:
    mem, msg = _seed(db_url)
    engine = build_engine(db_url)
    try:
        sql, p = _reply_sql(mem, msg)
        with engine.begin() as c:
            c.execute(text(sql), p)
        with pytest.raises(IntegrityError), engine.begin() as c:
            c.execute(text(sql), p)  # same gmail_message_id
        with engine.begin() as c:
            c.execute(text("INSERT INTO suppressions (address, reason, created_at) VALUES ('a@b.example', 'manual', :n)"), {"n": NOW})
        with pytest.raises(IntegrityError), engine.begin() as c:
            c.execute(text("INSERT INTO suppressions (address, reason, created_at) VALUES ('a@b.example', 'bounce', :n)"), {"n": NOW})
        with engine.begin() as c:
            c.execute(text("INSERT INTO reply_poll_state (member_id) VALUES (:m)"), {"m": mem})
        with pytest.raises(IntegrityError), engine.begin() as c:
            c.execute(text("INSERT INTO reply_poll_state (member_id) VALUES (:m)"), {"m": mem})
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "kind,intent",
    [("spam", "other"), ("human", "angry"), ("", None), ("HUMAN", "other")],
)
def test_check_constraints_reject_bad_reply_values(db_url: str, kind: str, intent: str | None) -> None:
    mem, msg = _seed(db_url)
    engine = build_engine(db_url)
    try:
        sql, p = _reply_sql(mem, msg, kind=kind, intent=intent)
        with pytest.raises(IntegrityError), engine.begin() as c:
            c.execute(text(sql), p)
        for good_kind, good_intent in (("human", None), ("bounce", None), ("auto_reply", None), ("human", "unsubscribe")):
            sql, p = _reply_sql(mem, msg, gmail_id=f"ok-{good_kind}-{good_intent}", kind=good_kind, intent=good_intent)
            with engine.begin() as c:
                c.execute(text(sql), p)
    finally:
        engine.dispose()


def test_check_constraint_rejects_a_bad_suppression_reason(db_url: str) -> None:
    engine = build_engine(db_url)
    try:
        with pytest.raises(IntegrityError), engine.begin() as c:
            c.execute(text("INSERT INTO suppressions (address, reason, created_at) VALUES ('a@b.example', 'because', :n)"), {"n": NOW})
        for reason in ("unsubscribe", "bounce", "manual"):
            with engine.begin() as c:
                c.execute(text("INSERT INTO suppressions (address, reason, created_at) VALUES (:a, :r, :n)"), {"a": f"{reason}@b.example", "r": reason, "n": NOW})
    finally:
        engine.dispose()


def test_downgrade_removes_only_the_reply_objects_and_upgrade_works_again(db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.downgrade(cfg, P3)
    tables, cols = _insp(db_url)
    assert tables & P5_TABLES == set() and cols & P5_COLUMNS == set()
    assert {"outreach_messages", "outreach_events", "team_members"} <= tables
    engine = build_engine(db_url)
    try:
        assert current_revision(engine) == P3
    finally:
        engine.dispose()
    command.upgrade(cfg, "head")
    tables, cols = _insp(db_url)
    assert P5_TABLES <= tables and P5_COLUMNS <= cols
    engine = build_engine(db_url)
    try:
        assert current_revision(engine) == head_revision()
    finally:
        engine.dispose()


def test_existing_outreach_rows_survive_the_upgrade_with_reply_status_none(db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.downgrade(cfg, P3)
    mem, msg = _seed(db_url)  # a row written under the old schema
    command.upgrade(cfg, "head")
    engine = build_engine(db_url)
    try:
        with engine.connect() as c:
            row = c.execute(text("SELECT id, state, subject, reply_status, last_reply_at, follow_up_of_id FROM outreach_messages")).one()
        assert tuple(row) == (msg, "sent", "S", "none", None, None)
        assert mem
    finally:
        engine.dispose()
