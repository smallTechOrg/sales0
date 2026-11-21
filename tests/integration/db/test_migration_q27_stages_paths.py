"""Migration q27 (SMA-17, SMA-33): the retired stages are mapped (Follow-up 1 / 2 -> Follow-up; Meeting scheduled,
Assessment, Proposal sent, Interviews -> Engaged), the CHECKs follow, companies get their enquiry / outreach path, and
the SQLite table rebuilds lose nothing (no ON DELETE action fires: every link is still there)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from sqlalchemy import text

from sales0.db.engine import build_engine
from sales0.db.migrate_check import alembic_config
from tests.integration.db._drift import drift
from tests.integration.db.test_migration_q17_company_model import NOW, _company, _enquiry, _lead, _prospect

PARENT = "q26_customer_wording_6f1a3c58"
REV = "q27_stages_paths_4c8e1a52"
COUNTED = ("prospects", "leads", "enquiries", "follow_ups", "companies")


def _seed(url: str) -> dict[str, int]:
    engine = build_engine(url)
    try:
        with engine.begin() as c:
            for cid, name in ((1, "Inbound"), (2, "Cold"), (3, "LeadCo"), (4, "Proposal"), (5, "Known")):
                _company(c, cid, name)
            _prospect(c, 1, "Inbound", 1, stage="follow_up_2")
            _prospect(c, 2, "Cold", 2, stage="follow_up_1")
            _prospect(c, 3, "LeadCo", 3, stage="contacted", status="converted")
            _prospect(c, 4, "Proposal", 4, stage="contacted", status="converted")
            _lead(c, 1, 3, "meeting_scheduled")
            _lead(c, 2, 4, "proposal_sent")
            c.execute(text("UPDATE leads SET company_id = prospect_id"))
            _enquiry(c, 1, prospect=1, company=1)
            _enquiry(c, 2, prospect=3, company=3)
            c.execute(text("UPDATE enquiries SET deal_status = 'follow_up_2' WHERE id = 1"))
            c.execute(text("UPDATE enquiries SET deal_status = 'interviews' WHERE id = 2"))
            for cid, stage, sub in ((1, "prospect", "follow_up_2"), (2, "prospect", "follow_up_1"), (3, "lead", "meeting_scheduled"),
                                    (4, "lead", "proposal_sent")):
                c.execute(text("UPDATE companies SET stage = :s, sub_status = :u WHERE id = :i"), {"s": stage, "u": sub, "i": cid})
            c.execute(text("INSERT INTO follow_ups (prospect_id, kind, due_on, created_at, updated_at) VALUES (1, 'follow_up_2', '2026-10-09', :d, :d)"),
                      {"d": NOW})
            return {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in COUNTED}
    finally:
        engine.dispose()


def test_upgrade_maps_stages_and_paths_without_loss(db_url: str, tmp_path: Path) -> None:
    sqlite = db_url.startswith("sqlite")
    url = f"sqlite:///{tmp_path / 'q27.db'}" if sqlite else db_url
    cfg = alembic_config(url)
    if sqlite:
        command.upgrade(cfg, PARENT)
    else:
        command.downgrade(cfg, PARENT)
    before = _seed(url)
    command.upgrade(cfg, REV)
    engine = build_engine(url)
    try:
        with engine.connect() as c:
            assert {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in COUNTED} == before
            assert dict(c.execute(text("SELECT id, stage FROM prospects")).all()) == {1: "follow_up", 2: "follow_up", 3: "contacted", 4: "contacted"}
            assert dict(c.execute(text("SELECT id, stage FROM leads")).all()) == {1: "engaged", 2: "engaged"}
            assert dict(c.execute(text("SELECT id, deal_status FROM enquiries")).all()) == {1: "follow_up", 2: "engaged"}
            co = {r[0]: (r[1], r[2]) for r in c.execute(text("SELECT name, sub_status, pipeline_path FROM companies"))}
            assert co == {"Inbound": ("follow_up", "enquiry"), "Cold": ("follow_up", "outreach"), "LeadCo": ("engaged", "enquiry"),
                          "Proposal": ("engaged", "outreach"), "Known": (None, None)}
            # the rebuilds fired no ON DELETE: the enquiry -> prospect links and the reminder are all still there
            assert dict(c.execute(text("SELECT id, prospect_id FROM enquiries")).all()) == {1: 1, 2: 3}
            assert c.execute(text("SELECT prospect_id FROM follow_ups")).scalar() == 1
            assert c.execute(text("SELECT COUNT(*) FROM leads WHERE prospect_id IN (3, 4)")).scalar() == 2
            if sqlite:  # the partial unique indexes survived with their WHERE clause
                sql = dict(c.execute(text("SELECT name, sql FROM sqlite_master WHERE type = 'index'")).all())
                assert "WHERE origin_enquiry_id IS NOT NULL" in sql["uq_prospects_origin_enquiry"]
                assert "WHERE closed_at IS NULL" in sql["uq_leads_one_open_per_prospect"]
        with engine.begin() as c:  # the new CHECK: a retired stage is refused, the new default is Qualified
            try:
                with c.begin_nested():
                    c.execute(text("UPDATE prospects SET stage = 'follow_up_1' WHERE id = 1"))
                raise AssertionError("the retired stage was accepted")
            except Exception as exc:  # noqa: BLE001
                assert "CHECK" in str(exc).upper() or "check" in str(exc)
            c.execute(text("INSERT INTO leads (prospect_id, stage_source, opened_at, opened_by, actor, created_at, updated_at) "
                           "VALUES (2, 'app', :d, 'manual', 'system', :d, :d)"), {"d": NOW})
            assert c.execute(text("SELECT stage FROM leads WHERE prospect_id = 2")).scalar() == "qualified"
        command.upgrade(cfg, "head")
        assert drift(engine) == []
        command.downgrade(cfg, PARENT)
        with engine.connect() as c:  # lossy by nature, but nothing deleted
            assert dict(c.execute(text("SELECT id, stage FROM prospects")).all())[1] == "follow_up_1"
            assert c.execute(text("SELECT COUNT(*) FROM enquiries WHERE prospect_id IS NOT NULL")).scalar() == 2
        command.upgrade(cfg, "head")
        assert drift(engine) == []
    finally:
        engine.dispose()
