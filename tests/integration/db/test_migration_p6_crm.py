"""Migration p6_crm_5b2e7d14 on real SQLite files: fresh DB, upgrade of a p5 DB with data (nothing lost,
foreign keys intact, backfill correct), downgrade / upgrade round trip, schema == models."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import inspect, text

from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, current_revision, head_revision
from tests.integration.db._drift import assert_inline_fks_cascade, drift

P5 = "p5_replies_c3d81f20"
P6 = "p6_crm_5b2e7d14"
NEW_TABLES = {
    "companies", "contacts", "contact_emails", "contact_phones", "tags", "contact_tags", "contact_links", "prospects", "leads",
    "clients", "follow_ups", "flags", "timeline_events", "competitors", "competitor_mentions", "channel_groups", "import_batches",
    "sheet_sources", "sheet_metrics", "sheet_metric_weeks",
}
OLD_COUNTED = (
    "enquiries", "enquiry_sources", "enquiry_keyword_hits", "triage_decisions", "seen_posts", "linkedin_profiles", "team_members",
    "outreach_messages", "outreach_events", "outreach_replies", "company_briefs", "status_history", "llm_calls", "offerings",
)
N = "2026-10-01 09:00:00.000000"
N_SENT = "2026-10-02 10:00:00.000000"


def _rows(url: str, sql: str, **params: object) -> list[sa.Row]:
    engine = build_engine(url)
    try:
        with engine.connect() as c:
            return list(c.execute(text(sql), params).all())
    finally:
        engine.dispose()


def _counts(url: str, tables: tuple[str, ...] = OLD_COUNTED) -> dict[str, int]:
    return {t: _rows(url, f"SELECT COUNT(*) FROM {t}")[0][0] for t in tables}


def _enquiry(c: sa.Connection, n: int, **kw: object) -> int:
    v: dict[str, object] = {
        "h": f"{n:064d}", "t": f"We need training #{n}", "rs": "accepted", "stage": "prospect_new", "da": N, "dec": N, "co": None,
        "cp": None, "em": None, "ph": None, "apid": None, "an": "Author", "des": None,
    }
    v.update(kw)
    c.execute(
        text(
            "INSERT INTO enquiries (source, content_hash, raw_text, date_added, review_status, stage, triage_status, decided_at, company,"
            " contact_person, email, phone, author_profile_id, author_name, date_email_sent, requirement_summary) "
            "VALUES ('linkedin_post', :h, :t, :da, :rs, :stage, 'done', :dec, :co, :cp, :em, :ph, :apid, :an, :des, :t)"
        ),
        v,
    )
    return int(c.execute(text("SELECT MAX(id) FROM enquiries")).scalar_one())


@pytest.fixture
def p5_db(tmp_path):  # type: ignore[no-untyped-def]
    """A DB at p5 head: 2 accepted enquiries from author A, 1 from B (email, no profile), a pending one, 1 contacted via a sent
    message, plus messages (one with a dangling enquiry id), events, a reply and a brief carrying Phase 3's meaningless ids."""
    url = f"sqlite:///{(tmp_path / 'p5.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, P5)
    engine = build_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO team_members (name, email, role, active, created_at) VALUES ('Ann', 'ann@t.example', 'member', 1, :n)"), {"n": N})
        for slug in ("in/author-a", "in/author-b"):
            c.execute(
                text("INSERT INTO linkedin_profiles (slug, kind, profile_url, display_name, first_seen_at, last_seen_at, posts_seen, relevant_count)"
                     " VALUES (:s, 'person', :u, :d, :n, :n, 3, 1)"),
                {"s": slug, "u": f"https://www.linkedin.com/{slug}/", "d": slug.upper(), "n": N},
            )
        pa, pb = (r[0] for r in c.execute(text("SELECT id FROM linkedin_profiles ORDER BY id")))
        e1 = _enquiry(c, 1, apid=pa, co="Acme Training", cp="Asha Rao", em="Asha@Acme.example", ph="98765 43210")
        e2 = _enquiry(c, 2, apid=pa, co="  acme   training ", em="asha@acme.example", dec="2026-10-03 09:00:00.000000", da="2026-10-03 09:00:00.000000")
        e3 = _enquiry(c, 3, apid=pb, co="Beta Ltd", cp="Bela B", des="2026-09-20")
        e4 = _enquiry(c, 4, rs="pending_review", stage=None, apid=pb, co="Beta Ltd")
        e5 = _enquiry(c, 5, co="No Profile Co", an="Nobody")
        c.execute(text("INSERT INTO status_history (entity_type, entity_id, field, from_value, to_value, changed_at) VALUES ('enquiry', :e, 'review_status', 'pending_review', 'accepted', :n)"), {"e": e1, "n": N})
        msgs: dict[str, int] = {}
        for key, vals in {
            "sent": dict(enq=e1, st="sent", pid=None, cid=None, sent=N_SENT),
            "dangling": dict(enq=9999, st="draft", pid=77, cid=88, sent=None),
            "plain": dict(enq=None, st="draft", pid=5, cid=6, sent=None),
        }.items():
            c.execute(
                text("INSERT INTO outreach_messages (member_id, connection_email, from_addr, to_addrs, cc_addrs, subject, body, state, enquiry_id,"
                     " prospect_id, company_id, sent_at, send_outcome_unknown, reply_status, created_at, updated_at)"
                     " VALUES (1, 'ann@t.example', 'ann@t.example', 'asha@acme.example', '', 'Hello Acme', 'Body', :st, :enq, :pid, :cid, :sent, 0, 'none', :n, :n)"),
                {**vals, "n": N},
            )
            msgs[key] = int(c.execute(text("SELECT MAX(id) FROM outreach_messages")).scalar_one())
        c.execute(text("UPDATE outreach_messages SET follow_up_of_id = :a WHERE id = :b"), {"a": msgs["sent"], "b": msgs["plain"]})
        for i in range(3):
            c.execute(text("INSERT INTO outreach_events (message_id, kind, created_at) VALUES (:m, 'sent', :n)"), {"m": msgs["sent"], "n": N})
        c.execute(
            text("INSERT INTO outreach_replies (message_id, member_id, gmail_message_id, gmail_thread_id, from_addr, received_at, snippet, kind, intent,"
                 " classified_by, handled, created_at) VALUES (:m, 1, 'g1', 't1', 'asha@acme.example', :n, 's', 'human', 'interested', 'llm', 0, :n)"),
            {"m": msgs["sent"], "n": N},
        )
        c.execute(text("INSERT INTO company_briefs (company_name, brief_json, created_at, cost_usd_micros, enquiry_id, prospect_id) VALUES ('Acme', '{}', :n, 0, 9999, 42)"), {"n": N})
    ids = {"e1": e1, "e2": e2, "e3": e3, "e4": e4, "e5": e5, **{f"m_{k}": v for k, v in msgs.items()}}
    engine.dispose()
    return {"url": url, "cfg": cfg, "ids": ids}


# ---------------------------------------------------------------- fresh DB
@pytest.mark.sqlite_only
def test_fresh_upgrade_reaches_the_single_head_and_matches_the_models(db_url: str) -> None:
    engine = build_engine(db_url)
    try:
        assert current_revision(engine) == head_revision()  # head moves on (p7+); P6 stays an ancestor
        insp = inspect(engine)
        assert NEW_TABLES <= set(insp.get_table_names())
        assert drift(engine) == []
        assert_inline_fks_cascade(engine)
        fks = {(fk["constrained_columns"][0], fk["referred_table"], fk["options"].get("ondelete")) for fk in insp.get_foreign_keys("outreach_messages")}
        assert {("prospect_id", "prospects", "SET NULL"), ("enquiry_id", "enquiries", "SET NULL"), ("company_id", "companies", "SET NULL"),
                ("follow_up_of_id", "outreach_messages", "SET NULL")} <= fks
        brief_fks = {(fk["constrained_columns"][0], fk["referred_table"]) for fk in insp.get_foreign_keys("company_briefs")}
        assert {("prospect_id", "prospects"), ("enquiry_id", "enquiries")} <= brief_fks
        ecols = {c["name"] for c in insp.get_columns("enquiries")}
        assert {"prospect_id", "company_id", "contact_id", "origin", "sheet_status", "sheet_notes", "sheet_removed_at", "sheet_fields"} <= ecols
        with engine.connect() as c:
            assert c.execute(text("PRAGMA foreign_key_check")).fetchall() == []
            assert sorted(r[0] for r in c.execute(text("SELECT name FROM competitors"))) == sorted(
                ["Evolvv", "Future Labs", "Ritu Arora", "Savya Technologies", "White Spaces"])
            idx = {r[1] for r in c.execute(text("PRAGMA index_list(leads)"))} | {r[1] for r in c.execute(text("PRAGMA index_list(flags)"))}
            assert {"uq_leads_one_open_per_prospect", "uq_flags_active"} <= idx
    finally:
        engine.dispose()


def test_the_partial_unique_indexes_and_checks_hold(db_url: str) -> None:
    from sqlalchemy.exc import IntegrityError

    engine = build_engine(db_url)
    try:
        with engine.begin() as c:
            c.execute(text("INSERT INTO prospects (name, created_at, updated_at) VALUES ('P', :n, :n)"), {"n": N})
            c.execute(text("INSERT INTO leads (prospect_id, opened_at, created_at, updated_at) VALUES (1, :n, :n, :n)"), {"n": N})
            c.execute(text("INSERT INTO leads (prospect_id, opened_at, closed_at, stage, created_at, updated_at) VALUES (1, :n, :n, 'lost', :n, :n)"), {"n": N})
            c.execute(text("INSERT INTO flags (entity_type, entity_id, kind, created_at, updated_at) VALUES ('prospect', 1, 'repeat_poster', :n, :n)"), {"n": N})
            c.execute(text("INSERT INTO flags (entity_type, entity_id, kind, cleared_at, created_at, updated_at) VALUES ('prospect', 1, 'repeat_poster', :n, :n, :n)"), {"n": N})
        for sql in (
            "INSERT INTO leads (prospect_id, opened_at, created_at, updated_at) VALUES (1, :n, :n, :n)",  # second OPEN lead
            "INSERT INTO flags (entity_type, entity_id, kind, created_at, updated_at) VALUES ('prospect', 1, 'repeat_poster', :n, :n)",
            "INSERT INTO prospects (name, stage, created_at, updated_at) VALUES ('Bad', 'won', :n, :n)",  # stage CHECK
            "INSERT INTO follow_ups (kind, due_on, created_at, updated_at) VALUES ('custom', '2026-10-05', :n, :n)",  # no owner
            "INSERT INTO follow_ups (prospect_id, lead_id, kind, due_on, created_at, updated_at) VALUES (1, 1, 'custom', '2026-10-05', :n, :n)",
        ):
            with pytest.raises(IntegrityError):
                with engine.begin() as c:
                    c.execute(text(sql), {"n": N})
    finally:
        engine.dispose()


# ---------------------------------------------------------------- upgrade of a populated p5 DB
def test_upgrade_loses_no_row_and_keeps_the_children_of_outreach_messages(p5_db) -> None:  # type: ignore[no-untyped-def]
    url, cfg = p5_db["url"], p5_db["cfg"]
    before = _counts(url)
    assert before["outreach_events"] == 3 and before["outreach_replies"] == 1 and before["outreach_messages"] == 3
    command.upgrade(cfg, "head")
    assert _counts(url) == before
    engine = build_engine(url)
    try:
        assert current_revision(engine) == head_revision()
        with engine.connect() as c:
            assert c.execute(text("PRAGMA foreign_key_check")).fetchall() == []
            assert c.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert drift(engine) == []
    finally:
        engine.dispose()
    # the self-reference survived the rebuild
    ids = p5_db["ids"]
    assert _rows(url, "SELECT follow_up_of_id FROM outreach_messages WHERE id = :i", i=ids["m_plain"])[0][0] == ids["m_sent"]


def test_phase3_crm_ids_are_nulled_and_rederived(p5_db) -> None:  # type: ignore[no-untyped-def]
    url, ids = p5_db["url"], p5_db["ids"]
    command.upgrade(p5_db["cfg"], "head")
    dangling = _rows(url, "SELECT enquiry_id, prospect_id, company_id FROM outreach_messages WHERE id = :i", i=ids["m_dangling"])[0]
    plain = _rows(url, "SELECT enquiry_id, prospect_id, company_id FROM outreach_messages WHERE id = :i", i=ids["m_plain"])[0]
    assert tuple(dangling) == (None, None, None) and tuple(plain) == (None, None, None)
    sent = _rows(url, "SELECT m.enquiry_id, m.prospect_id, m.company_id, e.prospect_id, e.company_id FROM outreach_messages m "
                      "JOIN enquiries e ON e.id = m.enquiry_id WHERE m.id = :i", i=ids["m_sent"])[0]
    assert sent[0] == ids["e1"] and sent[1] == sent[3] is not None and sent[2] == sent[4] is not None
    brief = _rows(url, "SELECT enquiry_id, prospect_id FROM company_briefs")[0]
    assert tuple(brief) == (None, None)


def test_backfill_builds_one_prospect_per_author_with_contact_company_timeline(p5_db) -> None:  # type: ignore[no-untyped-def]
    url, ids = p5_db["url"], p5_db["ids"]
    command.upgrade(p5_db["cfg"], "head")
    prospects = _rows(url, "SELECT id, name, linkedin_profile_id, stage, status, contacted_at, contacted_estimated, source, kind, origin_enquiry_id,"
                           " company_id, contact_id, actor FROM prospects ORDER BY id")
    assert len(prospects) == 3  # author A (2 enquiries), author B (1 accepted), the profile-less one
    by_origin = {p.origin_enquiry_id: p for p in prospects}
    pa, pb, pn = by_origin[ids["e1"]], by_origin[ids["e3"]], by_origin[ids["e5"]]
    assert pa.name == "Acme Training" and pa.stage == "contacted" and pa.contacted_estimated == 0 and str(pa.contacted_at).startswith("2026-10-02 10:00")
    assert pb.stage == "contacted" and pb.contacted_estimated == 1 and str(pb.contacted_at).startswith("2026-09-20")  # date-only => estimated
    assert pn.stage == "new" and pn.linkedin_profile_id is None and pn.contacted_at is None
    assert {p.status for p in prospects} == {"open"} and {p.source for p in prospects} == {"enquiry"} and {p.kind for p in prospects} == {"company_enquiry"}
    assert {p.actor for p in prospects} == {"backfill"}
    enq = {r.id: r for r in _rows(url, "SELECT id, prospect_id, company_id, contact_id FROM enquiries")}
    assert enq[ids["e1"]].prospect_id == enq[ids["e2"]].prospect_id == pa.id
    assert enq[ids["e3"]].prospect_id == pb.id and enq[ids["e4"]].prospect_id is None  # a pending enquiry gets no prospect
    assert enq[ids["e1"]].company_id == enq[ids["e2"]].company_id  # "  acme   training " == "Acme Training"
    assert enq[ids["e1"]].contact_id == enq[ids["e2"]].contact_id is not None  # same e-mail, case-insensitive
    assert [r[0] for r in _rows(url, "SELECT name_key FROM companies ORDER BY name_key")] == ["acme training", "beta ltd", "no profile co"]
    tracked = {r.slug: r.tracked for r in _rows(url, "SELECT slug, tracked FROM linkedin_profiles")}
    assert tracked == {"in/author-a": 1, "in/author-b": 1}
    contacts = _rows(url, "SELECT id, full_name, email, phone FROM contacts ORDER BY id")
    assert [c.email for c in contacts if c.email] == ["asha@acme.example"] and any(c.full_name == "Bela B" for c in contacts)
    assert _rows(url, "SELECT digits FROM contact_phones")[0][0] == "9876543210"
    tl = [tuple(r) for r in _rows(url, "SELECT kind, prospect_id FROM timeline_events ORDER BY id")]
    assert sorted(k for k, _ in tl) == ["created", "created", "created", "email_sent", "enquiry_linked"]
    assert {r[0] for r in _rows(url, "SELECT DISTINCT actor FROM timeline_events")} == {"backfill"}
    assert _rows(url, "SELECT COUNT(*) FROM follow_ups")[0][0] == 0 and _rows(url, "SELECT COUNT(*) FROM flags")[0][0] == 0
    assert _rows(url, "SELECT COUNT(*) FROM competitors")[0][0] == 5
    assert _rows(url, "SELECT COUNT(*) FROM leads")[0][0] == 0


def test_downgrade_then_upgrade_round_trips_with_data(p5_db) -> None:  # type: ignore[no-untyped-def]
    url, cfg = p5_db["url"], p5_db["cfg"]
    before = _counts(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, P5)  # not "-1": later revisions sit above p6
    engine = build_engine(url)
    try:
        assert current_revision(engine) == P5
        insp = inspect(engine)
        assert not (NEW_TABLES & set(insp.get_table_names()))
        assert "prospect_id" not in {c["name"] for c in insp.get_columns("enquiries")}
        assert {fk["constrained_columns"][0] for fk in insp.get_foreign_keys("outreach_messages")} == {"member_id", "approved_by_member_id", "follow_up_of_id"}
        with engine.connect() as c:
            assert c.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    finally:
        engine.dispose()
    assert _counts(url) == before
    command.upgrade(cfg, "head")
    assert _counts(url) == before
    engine = build_engine(url)
    try:
        assert drift(engine) == []
        assert current_revision(engine) == head_revision()
    finally:
        engine.dispose()


def test_full_downgrade_to_base_through_p5_with_data_then_upgrade(p5_db) -> None:  # type: ignore[no-untyped-def]
    """p5's own downgrade drops the self-FK by name: the p6 rebuild must keep that name."""
    url, cfg = p5_db["url"], p5_db["cfg"]
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "p4_ops_c3d8e1f2")
    command.upgrade(cfg, "head")
    engine = build_engine(url)
    try:
        assert drift(engine) == [] and current_revision(engine) == head_revision()
        names = {fk["name"] for fk in inspect(engine).get_foreign_keys("outreach_messages")}
        assert "fk_outreach_messages_follow_up_of_id_outreach_messages" in names
    finally:
        engine.dispose()


def test_empty_p5_database_upgrades_and_has_only_the_seeds(tmp_path) -> None:  # type: ignore[no-untyped-def]
    url = f"sqlite:///{(tmp_path / 'e.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, P5)
    command.upgrade(cfg, "head")
    assert _rows(url, "SELECT COUNT(*) FROM prospects")[0][0] == 0 and _rows(url, "SELECT COUNT(*) FROM competitors")[0][0] == 5
    command.downgrade(cfg, P5)
    command.upgrade(cfg, "head")
    assert _rows(url, "SELECT COUNT(*) FROM competitors")[0][0] == 5


def test_the_migration_module_declares_the_recipe_and_the_chain() -> None:
    import importlib.util
    from pathlib import Path

    from sales0.config import REPO_ROOT

    spec = importlib.util.spec_from_file_location("p6_mod", Path(REPO_ROOT) / "migrations/versions/p6_crm_5b2e7d14.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._CHILDREN == ("outreach_events", "outreach_replies", "outreach_messages")  # the counts the recipe verifies
    assert mod.revision == P6 and mod.down_revision == P5
    doc = mod.__doc__ or ""
    assert "foreign_keys=OFF" in doc and "PRAGMA" in doc  # the one-line justification required by "Migrations (binding)"
