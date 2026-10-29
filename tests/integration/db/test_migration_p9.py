"""Migrations p9_reply_feat / p9_activity / p9_originuq (audit @7ae3d66 #6, #19, #2)."""

from __future__ import annotations

import sqlalchemy as sa

from datetime import UTC, date, datetime

import pytest
from alembic import command
import itertools
from types import SimpleNamespace

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, current_revision, head_revision
from sales0.db.models import (
    Client, ContactLink, Enquiry, Flag, Lead, Prospect,
    SheetSource, TeamMember, TimelineEvent,
)
from tests.integration.db._drift import assert_inline_fks_cascade, drift

P8 = "p8_liaccts_b2e5f7a9"
P_REPLY = "p9_reply_feat_c1e7a3b5"
P_ACT = "p9_activity_d4a8c2e6"
P_UQ = "p9_originuq_b7f2d9a1"
INSERT_LLM = (
    "INSERT INTO llm_calls (feature, model, status, prompt_version, cost_usd_micros, created_at) "
    "VALUES (:f, 'm', 'ok', :pv, :c, '2026-10-01 00:00:00')"
)


@pytest.fixture
def at_p8(tmp_path):  # type: ignore[no-untyped-def]
    url = f"sqlite:///{(tmp_path / 'p8.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, P8)
    return url, cfg


def _rows(url: str, sql: str) -> list:
    engine = build_engine(url)
    try:
        with engine.connect() as c:
            return list(c.execute(text(sql)).mappings().all())
    finally:
        engine.dispose()


# ----------------------------------------------------------------------------------------- reply feature
def test_reply_feature_check_relabels_old_reply_rows_and_round_trips(at_p8) -> None:  # type: ignore[no-untyped-def]
    url, cfg = at_p8
    engine = build_engine(url)
    with engine.begin() as c:
        c.execute(text(INSERT_LLM), {"f": "triage", "pv": "reply-v1", "c": 5})  # a reply call booked as triage
        c.execute(text(INSERT_LLM), {"f": "triage", "pv": "triage-v3", "c": 7})
        with pytest.raises(IntegrityError):
            with c.begin_nested():
                c.execute(text(INSERT_LLM), {"f": "reply", "pv": "x", "c": 1})  # not allowed before the migration
    engine.dispose()
    command.upgrade(cfg, P_REPLY)
    got = {r["pv"]: r["f"] for r in _rows(url, "SELECT feature AS f, prompt_version AS pv FROM llm_calls")}
    assert got == {"reply-v1": "reply", "triage-v3": "triage"}
    engine = build_engine(url)
    try:
        with engine.begin() as c:
            c.execute(text(INSERT_LLM), {"f": "reply", "pv": "reply-v1", "c": 1})
        with pytest.raises(IntegrityError), engine.begin() as c:
            c.execute(text(INSERT_LLM), {"f": "bogus", "pv": "x", "c": 1})
    finally:
        engine.dispose()
    command.downgrade(cfg, P8)
    assert {r["f"] for r in _rows(url, "SELECT feature AS f FROM llm_calls")} == {"triage"}
    assert _rows(url, "SELECT COUNT(*) AS n FROM llm_calls")[0]["n"] == 3  # spend history kept
    command.upgrade(cfg, "head")


# ----------------------------------------------------------------------------------------- activity id
def test_activity_id_is_backfilled_from_the_existing_references(at_p8) -> None:  # type: ignore[no-untyped-def]
    url, cfg = at_p8
    command.upgrade(cfg, P_REPLY)
    engine = build_engine(url)
    ids: list[int] = []
    with engine.begin() as c:
        for n, link in enumerate((
            "https://www.linkedin.com/feed/update/urn:li:activity:7300000000000000001/",
            "https://www.linkedin.com/in/x/",  # link is a profile: the id lives only in its source
            "https://www.linkedin.com/posts/y_hello-activity-7300000000000000003-AbCd",
            None,  # nothing to extract
        )):
            ids.append(c.execute(text(
                "INSERT INTO enquiries (content_hash, raw_text, source_url, date_added, triage_status) "
                "VALUES (:h, 't', :u, '2026-10-01 09:00:00.000000', 'done') RETURNING id"), {"h": f"{n:064d}", "u": link}).scalar_one())
        c.execute(text(
            "INSERT INTO enquiry_sources (enquiry_id, source_ref, first_seen_at) "
            "VALUES (:e, 'urn:li:activity:7300000000000000002', '2026-10-01 09:00:00.000000')"), {"e": ids[1]})
    engine.dispose()
    command.upgrade(cfg, P_ACT)
    got = {r["id"]: r["a"] for r in _rows(url, "SELECT id, activity_id AS a FROM enquiries")}
    assert got == {
        ids[0]: "7300000000000000001", ids[1]: "7300000000000000002", ids[2]: "7300000000000000003", ids[3]: None,
    }
    assert "ix_enquiries_activity_id" in {r["name"] for r in _rows(url, "SELECT name FROM sqlite_master WHERE type='index'")}
    command.downgrade(cfg, P_REPLY)
    assert "activity_id" not in {r["name"] for r in _rows(url, "PRAGMA table_info(enquiries)")}
    command.upgrade(cfg, "head")
    assert {r["id"]: r["a"] for r in _rows(url, "SELECT id, activity_id AS a FROM enquiries")} == got


# ----------------------------------------------------------------------------------------- origin dedupe
_ENQ = itertools.count(1)


def _old_enquiry(s: Session, **kw: object) -> SimpleNamespace:
    """An enquiry row written with Core and ONLY these columns: the ORM model also maps columns that later
    migrations add (e.g. q14's pipeline_*), which do not exist yet at this revision."""
    n = next(_ENQ)
    values: dict[str, object] = {
        "content_hash": f"{n:064d}", "raw_text": f"We need AI training for our managers, post {n}.",
        "review_status": "accepted", "stage": "prospect_new", "triage_status": "done", "author_name": "Asha Rao",
        "date_added": datetime(2026, 10, 1, 9, 0, tzinfo=UTC), **kw,
    }
    # a bare table of just these columns: Python-side defaults of columns added later (q14, q16 category ...) must not apply
    table = sa.table("enquiries", sa.column("id"), *[sa.column(k) for k in values])
    return SimpleNamespace(id=s.execute(insert(table).values(**values).returning(table.c.id)).scalar_one())


def _old_prospect(s: Session, **kw: object) -> SimpleNamespace:
    """A prospect row written with Core and ONLY these columns (q16's priority does not exist at this revision)."""
    table = sa.table("prospects", sa.column("id"), *[sa.column(k) for k in kw])
    return SimpleNamespace(id=s.execute(insert(table).values(**kw).returning(table.c.id)).scalar_one())


def _seed_duplicates(url: str) -> dict[str, int]:
    engine = build_engine(url)
    t = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    with Session(engine) as s:
        m = TeamMember(name="Pat", email="pat@team.example", role="member")
        s.add(m)
        e = _old_enquiry(s, company="Beta Corp")
        other = _old_enquiry(s, company="Solo Ltd")
        keep = _old_prospect(s, name="Beta Corp", origin_enquiry_id=e.id, created_at=t, updated_at=t)
        lose = _old_prospect(
            s, name="Beta Corp", origin_enquiry_id=e.id, stage="contacted", status="open", contacted_at=t, notes="called once",
            created_at=t, updated_at=t,
        )
        lose2 = _old_prospect(s, name="Beta Corp", origin_enquiry_id=e.id, created_at=t, updated_at=t)
        solo = _old_prospect(s, name="Solo Ltd", origin_enquiry_id=other.id, created_at=t, updated_at=t)
        s.execute(update(Enquiry.__table__).where(Enquiry.__table__.c.id == e.id).values(prospect_id=lose.id))  # a LOSER
        k_lead = Lead(prospect_id=keep.id, opened_at=t)
        l_lead = Lead(prospect_id=lose.id, opened_at=t)  # second OPEN lead -> must be closed
        s.add_all([k_lead, l_lead])
        s.flush()
        # Core insert: the FollowUp model also maps q13's ``channel``, which does not exist yet at this revision.
        s.execute(insert(sa.table("follow_ups", *[sa.column(c) for c in ("prospect_id", "kind", "due_on", "note", "auto", "created_at", "updated_at")])).values(
            prospect_id=lose.id, kind="custom", due_on=date(2026, 11, 1), note="ring", auto=False,
            created_at=t, updated_at=t,
        ))
        s.add_all([
            Client(name="Beta", prospect_id=lose2.id),
            TimelineEvent(subject_type="prospect", subject_id=lose.id, prospect_id=lose.id, kind="note", body="hist",
                          actor="system", occurred_at=t, created_at=t),
            TimelineEvent(subject_type="lead", subject_id=l_lead.id, prospect_id=lose.id, kind="lead_created",
                          actor="system", occurred_at=t, created_at=t),
            Flag(entity_type="prospect", entity_id=lose.id, kind="repeat_poster"),
            Flag(entity_type="prospect", entity_id=keep.id, kind="repeat_poster"),
            SheetSource(entity="prospect", key="k", target_type="prospect", target_id=lose.id, sheet_id="s", gid="0",
                           tab_name="T", row_number=2, row_hash="x"),
        ])
        om = sa.table("outreach_messages", *[sa.column(x) for x in ("member_id", "connection_email", "from_addr", "prospect_id", "created_at", "updated_at")])
        s.execute(insert(om).values(member_id=m.id, connection_email=m.email, from_addr=m.email, prospect_id=lose.id, created_at=t, updated_at=t))
        c = _old_enquiry(s)  # a contact link target
        from sales0.db.models import Contact

        ct = Contact(full_name="Asha")
        s.add(ct)
        s.flush()
        s.add_all([
            ContactLink(contact_id=ct.id, entity_type="prospect", entity_id=lose.id, role="buyer"),
            ContactLink(contact_id=ct.id, entity_type="prospect", entity_id=keep.id, role="buyer"),  # duplicate link
        ])
        s.commit()
        out = {"enq": e.id, "keep": keep.id, "lose": lose.id, "lose2": lose2.id, "solo": solo.id, "lead_lose": l_lead.id,
               "other": c.id}
    engine.dispose()
    return out


def test_duplicates_are_merged_into_the_oldest_with_all_history_then_the_index_exists(at_p8) -> None:  # type: ignore[no-untyped-def]
    url, cfg = at_p8
    command.upgrade(cfg, P_ACT)
    ids = _seed_duplicates(url)
    command.upgrade(cfg, P_UQ)
    k = ids["keep"]
    assert [r["id"] for r in _rows(url, "SELECT id FROM prospects ORDER BY id")] == [k, ids["solo"]]
    only = _rows(url, f"SELECT * FROM prospects WHERE id = {k}")[0]
    assert only["stage"] == "contacted" and only["contacted_at"] is not None and "called once" in only["notes"]
    assert _rows(url, f"SELECT prospect_id AS p FROM enquiries WHERE id = {ids['enq']}")[0]["p"] == k
    for table, col in (("follow_ups", "prospect_id"), ("clients", "prospect_id"), ("outreach_messages", "prospect_id")):
        assert _rows(url, f"SELECT COUNT(*) AS n FROM {table} WHERE {col} = {k}")[0]["n"] == 1, table
    leads = _rows(url, "SELECT id, prospect_id, stage, closed_at FROM leads ORDER BY id")
    assert len(leads) == 2 and {r["prospect_id"] for r in leads} == {k}
    assert sum(1 for r in leads if r["closed_at"] is None) == 1
    closed = next(r for r in leads if r["id"] == ids["lead_lose"])
    assert closed["stage"] == "lost" and closed["closed_at"] is not None
    tl = _rows(url, "SELECT kind, subject_type, subject_id, prospect_id FROM timeline_events ORDER BY id")
    assert sum(1 for r in tl if r["prospect_id"] == k) == 4  # 2 moved + 2 merge notes
    assert all(r["subject_id"] == k for r in tl if r["subject_type"] == "prospect")
    assert _rows(url, "SELECT COUNT(*) AS n FROM flags WHERE entity_type='prospect' AND cleared_at IS NULL")[0]["n"] == 1
    assert _rows(url, "SELECT COUNT(*) AS n FROM flags WHERE entity_type='prospect' AND entity_id != %d" % k)[0]["n"] == 0
    assert _rows(url, "SELECT COUNT(*) AS n FROM contact_links WHERE entity_type='prospect' AND entity_id = %d" % k)[0]["n"] == 1
    assert _rows(url, "SELECT target_id AS t FROM sheet_sources")[0]["t"] == k
    assert _rows(url, "PRAGMA foreign_key_check") == []
    engine = build_engine(url)
    try:
        with Session(engine) as s, pytest.raises(IntegrityError):
            now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
            _old_prospect(s, name="again", origin_enquiry_id=ids["solo"], created_at=now, updated_at=now)
    finally:
        engine.dispose()
    command.downgrade(cfg, P_ACT)
    assert "uq_prospects_origin_enquiry" not in {r["name"] for r in _rows(url, "SELECT name FROM sqlite_master")}


def test_the_dedupe_is_a_noop_without_duplicates_and_upgrade_is_retry_safe(at_p8) -> None:  # type: ignore[no-untyped-def]
    url, cfg = at_p8
    command.upgrade(cfg, P_ACT)
    engine = build_engine(url)
    with Session(engine) as s:
        e = _old_enquiry(s)
        now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        _old_prospect(s, name="One", origin_enquiry_id=e.id, created_at=now, updated_at=now)
        _old_prospect(s, name="Free", origin_enquiry_id=None, created_at=now, updated_at=now)
        _old_prospect(s, name="Free2", origin_enquiry_id=None, created_at=now, updated_at=now)  # NULL origins never collide
        s.commit()
    engine.dispose()
    command.upgrade(cfg, P_UQ)
    command.downgrade(cfg, P_ACT)
    command.upgrade(cfg, P_UQ)
    assert _rows(url, "SELECT COUNT(*) AS n FROM prospects")[0]["n"] == 3


def test_head_matches_the_models_and_the_chain_is_linear(db_url: str) -> None:
    engine = build_engine(db_url)
    try:
        assert current_revision(engine) == head_revision()
        assert drift(engine) == []
        assert_inline_fks_cascade(engine)
        with engine.connect() as c:
            assert c.execute(select(func.count()).select_from(Prospect)).scalar_one() == 0
    finally:
        engine.dispose()
