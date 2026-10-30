"""Migration q17_company_model_c5e9a2d7: companies get stage / sub_status / off_ramp, enquiries a deal status, and every
existing prospect / lead / client / enquiry row is mapped without loss (counts before == after, ids untouched).
Runs on PostgreSQL too (``SALES0_TEST_POSTGRES_URL``) through the shared ``db_url`` fixture when set."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config, head_revision
from tests.integration.db._drift import drift

PARENT = "q16_feedback2_a41c7e92"
REV = "q17_company_model_c5e9a2d7"
NOW = "2026-10-01 00:00:00"
LATER = "2026-10-05 00:00:00"
COUNTED = ("prospects", "leads", "clients", "enquiries", "follow_ups", "contacts")


def _company(c, cid: int, name: str) -> None:
    c.execute(text("INSERT INTO companies (id, name, name_key, created_at, updated_at) VALUES (:i, :n, :k, :d, :d)"),
              {"i": cid, "n": name, "k": name.lower(), "d": NOW})


def _prospect(c, pid: int, name: str, company: int | None, *, stage: str = "new", status: str = "open",
              reason: str | None = None, kind: str = "company_enquiry") -> None:
    c.execute(text(
        "INSERT INTO prospects (id, kind, name, company_id, stage, stage_source, status, closed_reason, closed_at, source, "
        "contacted_estimated, priority_source, actor, created_at, updated_at) VALUES (:i, :k, :n, :c, :s, 'app', :st, :r, :ca, "
        "'enquiry', FALSE, 'derived', 'system', :d, :d)"
    ), {"i": pid, "k": kind, "n": name, "c": company, "s": stage, "st": status, "r": reason,
        "ca": LATER if status == "closed" else None, "d": NOW})


def _lead(c, lid: int, pid: int, stage: str, *, closed: bool = False) -> None:
    c.execute(text(
        "INSERT INTO leads (id, prospect_id, stage, stage_source, opened_at, closed_at, opened_by, actor, created_at, updated_at) "
        "VALUES (:i, :p, :s, 'app', :d, :ca, 'manual', 'system', :d, :d)"
    ), {"i": lid, "p": pid, "s": stage, "ca": LATER if closed else None, "d": NOW})


def _client(c, kid: int, name: str, company: int | None, *, lead: int | None = None, prospect: int | None = None) -> None:
    c.execute(text(
        "INSERT INTO clients (id, name, company_id, status, source, lead_id, prospect_id, actor, created_at, updated_at) "
        "VALUES (:i, :n, :c, 'active', 'manual', :l, :p, 'system', :d, :d)"
    ), {"i": kid, "n": name, "c": company, "l": lead, "p": prospect, "d": NOW})


def _enquiry(c, eid: int, *, prospect: int | None, company: int | None, review: str = "accepted") -> None:
    c.execute(text(
        "INSERT INTO enquiries (id, content_hash, raw_text, source, prospect_id, company_id, review_status, triage_status, "
        "date_added, origin, category) VALUES (:i, :h, 'x', 'linkedin_post', :p, :c, :r, 'done', :d, 'discovery', 'relevant_prospect')"
    ), {"i": eid, "h": f"{eid:064d}", "p": prospect, "c": company, "r": review, "d": NOW})


def seed(url: str) -> dict[str, int]:
    """Every prospect / lead / client / enquiry combination the app can hold at q16. Returns the row counts."""
    engine = build_engine(url)
    with engine.begin() as c:
        for cid, name in ((1, "Acme"), (2, "Beta"), (3, "Gamma"), (4, "Delta"), (5, "Echo"), (6, "Zeta"), (7, "Eta"), (8, "Theta")):
            _company(c, cid, name)
        _prospect(c, 1, "Acme", 1)                                          # new
        _prospect(c, 11, "Acme person two", 1, stage="contacted")           # second thread, more advanced
        _prospect(c, 2, "Beta", 2, stage="follow_up_1")
        _prospect(c, 3, "Gamma", 3, stage="contacted", status="converted")  # + open lead
        _lead(c, 1, 3, "meeting_scheduled")
        _prospect(c, 4, "Delta", 4, stage="contacted", status="converted")  # won lead + client
        _lead(c, 2, 4, "won", closed=True)
        _client(c, 1, "Delta", 4, lead=2, prospect=4)
        _client(c, 2, "Echo", 5)                                            # client without a prospect
        _client(c, 3, "Typed-in client", None)                              # client without a company
        _prospect(c, 5, "Zeta", 6, stage="final_follow_up", status="closed", reason="no_response")
        _prospect(c, 6, "Eta", 7, stage="contacted", status="closed", reason="not_interested")
        _prospect(c, 7, "Theta", 8, stage="contacted", status="converted")
        _lead(c, 3, 7, "lost", closed=True)
        _prospect(c, 8, "Jane Doe", None, kind="outreach_person")           # a person, no company
        _prospect(c, 9, "Mystery", None)
        # enquiries: accepted ones in every state, a pending one, a discarded one, accepted without prospect
        _enquiry(c, 1, prospect=1, company=None)
        _enquiry(c, 2, prospect=2, company=2)
        _enquiry(c, 3, prospect=3, company=3)
        _enquiry(c, 4, prospect=4, company=4)
        _enquiry(c, 5, prospect=5, company=6)
        _enquiry(c, 6, prospect=7, company=8)
        _enquiry(c, 7, prospect=None, company=None, review="pending_review")
        _enquiry(c, 8, prospect=None, company=None, review="discarded")
        _enquiry(c, 9, prospect=None, company=None)
        c.execute(text("INSERT INTO follow_ups (id, prospect_id, kind, due_on, auto, created_at, updated_at, planned_channel) "
                       "VALUES (1, 2, 'follow_up_1', '2026-10-05', TRUE, :d, :d, 'email')"), {"d": NOW})
        if c.dialect.name == "postgresql":  # explicit ids above: move the serial past them, as a live database would be
            c.execute(text("SELECT setval(pg_get_serial_sequence('companies', 'id'), (SELECT MAX(id) FROM companies))"))
        counts = {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in COUNTED}
        counts["companies"] = c.execute(text("SELECT COUNT(*) FROM companies")).scalar()
    engine.dispose()
    return counts


def test_upgrade_maps_every_row_without_loss(db_url: str, tmp_path: Path) -> None:
    sqlite = db_url.startswith("sqlite")
    url = f"sqlite:///{tmp_path / 'q17.db'}" if sqlite else db_url
    cfg = alembic_config(url)
    if sqlite:
        command.upgrade(cfg, PARENT)
    else:
        command.downgrade(cfg, PARENT)
    before = seed(url)
    command.upgrade(cfg, REV)
    command.upgrade(cfg, REV)  # re-runnable
    engine = build_engine(url)
    try:
        with engine.connect() as c:
            after = {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in COUNTED}
            # counts before == after for every ledger table (nothing deleted, nothing duplicated)
            assert {t: after[t] for t in COUNTED} == {t: before[t] for t in COUNTED}
            # companies: exactly the three companyless records got one (client "Typed-in client", "Jane Doe", "Mystery")
            assert c.execute(text("SELECT COUNT(*) FROM companies")).scalar() == before["companies"] + 3
            assert c.execute(text("SELECT COUNT(*) FROM prospects WHERE company_id IS NULL")).scalar() == 0
            assert c.execute(text("SELECT COUNT(*) FROM clients WHERE company_id IS NULL")).scalar() == 0
            assert c.execute(text("SELECT COUNT(*) FROM leads WHERE company_id IS NULL")).scalar() == 0
            st = {r[0]: tuple(r[1:]) for r in c.execute(text("SELECT name, stage, sub_status, off_ramp, resume_stage FROM companies"))}
            assert st["Acme"] == ("prospect", "contacted", None, None)        # most advanced thread wins
            assert st["Beta"] == ("prospect", "follow_up_1", None, None)
            assert st["Gamma"] == ("lead", "meeting_scheduled", None, None)
            assert st["Delta"] == ("client", "active", None, None)
            assert st["Echo"] == ("client", "active", None, None)
            assert st["Typed-in client"] == ("client", "active", None, None)
            assert st["Zeta"] == ("prospect", "final_follow_up", "dormant", "prospect")
            assert st["Eta"] == ("prospect", "contacted", "not_interested", "prospect")
            assert st["Theta"] == ("lead", "meeting_scheduled", "not_interested", "lead")
            assert st["Jane Doe"] == ("prospect", "new", None, None)
            assert st["Mystery"] == ("prospect", "new", None, None)
            deal = dict(c.execute(text("SELECT id, deal_status FROM enquiries")).all())
            assert deal == {1: "open", 2: "follow_up_1", 3: "meeting_scheduled", 4: "won", 5: "no_response", 6: "lost",
                            7: None, 8: None, 9: "open"}
            assert c.execute(text("SELECT company_id FROM enquiries WHERE id = 1")).scalar() == 1  # inherited from the prospect
        command.upgrade(cfg, "head")  # later schema revisions are part of the models drift is checked against
        assert drift(engine) == []
        command.downgrade(cfg, PARENT)
        insp = inspect(engine)
        assert "stage" not in {x["name"] for x in insp.get_columns("companies")}
        assert "deal_status" not in {x["name"] for x in insp.get_columns("enquiries")}
        with engine.connect() as c:  # the downgrade deletes nothing
            assert {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in COUNTED} == {t: before[t] for t in COUNTED}
        command.upgrade(cfg, "head")
        assert drift(engine) == []
    finally:
        engine.dispose()


def test_revision_chain() -> None:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    rev = script.get_revision(REV)
    assert rev is not None and rev.down_revision == PARENT
    assert REV in {r.revision for r in script.iterate_revisions(head_revision(), "base")}
