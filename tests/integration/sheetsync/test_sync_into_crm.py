"""The sheet -> CRM sync end to end on a real migrated SQLite DB (synthetic CSV, no network)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import func, select

from sales0.db.engine import session_scope
from sales0.db.models import Enquiry
from sales0.db.models.crm import Client, Company, Lead, Prospect
from sales0.db.models.registries import Competitor
from sales0.db.models.sheetdata import SheetMetric, SheetMetricWeek
from sales0.db.models.sheetdata import SheetSource as SheetSourceRow
from sales0.db.models.timeline import TimelineEvent
from sales0.services.sheetsync.sql_repository import SqlSheetRepository
from tests.integration.sheetsync.conftest import ACTIVE, NOW, matrix_csv, table_counts
from tests.unit.sheetsync import fixtures as fx


def assert_invariant(report) -> None:  # type: ignore[no-untyped-def]
    """No row lost without an error entry (spec/data.md)."""
    for name, t in report.tabs.items():
        assert t.fetch_error is None, name
        assert t.rows == t.added + t.updated + t.unchanged + len({e.row for e in t.errors}), (name, t.summary())


# --------------------------------------------------------------------------- first sync
def test_first_sync_fills_the_crm_and_loses_no_row(sync) -> None:  # type: ignore[no-untyped-def]
    report = sync()
    assert report.ok
    assert_invariant(report)
    t = report.tabs
    assert t["Active Enquiries"].added == 7 and len(t["Active Enquiries"].errors) == 1
    assert t["Competitors"].added == 3 and t["Groups"].added == 4 and t["Weekly"].added == 3 and t["Secondary"].added == 4
    c = table_counts()
    assert c["enquiries"] == 7 + 2 and c["sheet_metrics"] == 7
    assert c["competitors"] >= 3 and c["channel_groups"] == 4
    assert c["prospects"] >= 5 and c["companies"] > 0 and c["contacts"] > 0
    assert c["sheet_metric_weeks"] > 0
    with session_scope() as db:
        # Active rows are accepted, sheet-origin enquiries with a prospect each; Discarded rows are discarded
        active = db.scalars(select(Enquiry).where(Enquiry.origin == "sheet", Enquiry.review_status == "accepted")).all()
        assert len(active) == 7 and all(e.prospect_id for e in active) and all(e.triage_status == "done" and e.ai_verdict is None for e in active)
        gone = db.scalars(select(Enquiry).where(Enquiry.review_status == "discarded")).all()
        assert len(gone) == 2 and all(e.discard_reason for e in gone) and all(e.origin == "sheet" for e in gone)
        # raw sheet status is kept verbatim
        assert "Email Sent" in {e.sheet_status for e in active}
        # the duplicate "Week 43" column is kept as occurrence 1 and 2
        occ = db.scalars(select(SheetMetricWeek.occurrence).where(SheetMetricWeek.week_no == 43)).all()
        assert sorted(set(occ)) == [1, 2]
        # no reminders are created by a sync
        from sales0.db.models.crm import FollowUp

        assert db.scalar(select(func.count()).select_from(FollowUp)) == 0


def test_every_timeline_event_is_by_the_sync_and_holds_no_cell_values(sync) -> None:  # type: ignore[no-untyped-def]
    sync()
    with session_scope() as db:
        events = db.scalars(select(TimelineEvent)).all()
        # contact_linked / company 'created' are written by crm.prospects.ensure_for_enquiry (S1) with actor "system"
        assert events and all(e.actor == "sheet-sync" for e in events if e.kind != "contact_linked" and e.subject_type != "company")
        text = " ".join((e.body or "") for e in events if e.kind.startswith("sheet_")).lower()
        for secret in ("acme", "@", "linkedin.com", "asha test", "pat example", "98765"):
            assert secret not in text, secret
        kinds = {e.kind for e in events}
        assert "sheet_created" in kinds


def test_same_person_in_outreach_and_prospects_is_one_prospect_with_two_sources(sync) -> None:  # type: ignore[no-untyped-def]
    sync()
    with session_scope() as db:
        rows = db.scalars(select(SheetSourceRow).where(SheetSourceRow.entity == "prospect", SheetSourceRow.key == "li:pat-example-123")).all()
        assert len(rows) == 2 and len({r.target_id for r in rows}) == 1
        assert {r.tab_name for r in rows} == {"Outreach", "Prospects"}
        p = db.get(Prospect, rows[0].target_id)
        assert p.kind == "outreach_person" and p.source == "sheet" and p.linkedin_profile_id is not None
        assert p.stage == "contacted" and p.contacted_estimated is True  # "Message sent" in Outreach
        assert p.sheet_status == "Message sent" and p.sheet_notes  # blank Prospects status does not clear it
        assert p.stage_source == "sheet"


# --------------------------------------------------------------------------- idempotency / edits / removal
def test_second_sync_changes_nothing(sync) -> None:  # type: ignore[no-untyped-def]
    sync()
    before = table_counts()
    report = sync()
    assert_invariant(report)
    assert report.total_changes == 0
    assert all(t.added == t.updated == t.removed == 0 for t in report.tabs.values())
    assert table_counts() == before  # not even a timeline event


def test_dry_run_writes_nothing_and_predicts_the_real_run(sync) -> None:  # type: ignore[no-untyped-def]
    dry = sync(dry_run=True)
    assert table_counts()["enquiries"] == 0 and table_counts()["sheet_sources"] == 0
    real = sync()
    assert {n: (t.added, t.updated, t.unchanged, t.removed) for n, t in dry.tabs.items()} == {
        n: (t.added, t.updated, t.unchanged, t.removed) for n, t in real.tabs.items()
    }


def test_a_sheet_edit_updates_only_sheet_owned_fields(sync) -> None:  # type: ignore[no-untyped-def]
    texts = {"Active Enquiries": matrix_csv()}
    tabs = (ACTIVE,)
    sync(texts, tabs=tabs)
    with session_scope() as db:
        e = db.scalar(select(Enquiry).where(Enquiry.company == "Fornax"))
        eid, prospect_id = e.id, e.prospect_id
        e.notes = "our own note"  # CRM-owned
        p = db.get(Prospect, prospect_id)
        p.notes = "prospect note"
    edited = matrix_csv().replace("Fornax - Chatbots - Pune | Online", "Fornax - Chatbots - Nagpur | Online")
    report = sync({"Active Enquiries": edited}, tabs=tabs)
    t = report.tabs["Active Enquiries"]
    assert (t.added, t.updated, t.unchanged) == (0, 1, 9)
    assert_invariant(report)
    with session_scope() as db:
        e = db.get(Enquiry, eid)
        assert e.city == "Nagpur" and "Nagpur" in e.requirement_summary
        assert e.notes == "our own note"
        ev = db.scalars(select(TimelineEvent).where(TimelineEvent.enquiry_id == eid, TimelineEvent.kind == "sheet_updated")).all()
        assert ev and "requirement_summary" in ev[-1].body and "Nagpur" not in ev[-1].body
        assert db.get(Prospect, prospect_id).notes == "prospect note"


def test_a_cleared_cell_clears_the_column(sync) -> None:  # type: ignore[no-untyped-def]
    texts = {"Active Enquiries": matrix_csv()}
    sync(texts, tabs=(ACTIVE,))
    edited = matrix_csv().replace("d1@draco.example", "")
    sync({"Active Enquiries": edited}, tabs=(ACTIVE,))
    with session_scope() as db:
        assert db.scalar(select(Enquiry.email).where(Enquiry.company == "Draco")) is None


def test_removed_rows_are_soft_marked_then_restored(sync) -> None:  # type: ignore[no-untyped-def]
    sync()
    keep = fx.competitors_csv().splitlines()
    shorter = "\n".join(keep[:3]) + "\n"  # header + two of the three rivals (1 of 3 vanishes: under the guard)
    report = sync({**fx.all_tabs_csv(), "Competitors": shorter})
    assert report.tabs["Competitors"].removed == 1
    with session_scope() as db:
        gone = db.scalars(select(Competitor).where(Competitor.sheet_removed_at.is_not(None))).all()
        assert len(gone) == 1
        assert db.scalar(select(func.count()).select_from(Competitor)) >= 3  # never hard-deleted
        kinds = db.scalars(select(TimelineEvent.kind).where(TimelineEvent.subject_type == "competitor")).all()
        assert kinds.count("sheet_removed") == 1
    back = sync()
    assert back.tabs["Competitors"].added == 1  # the row is back
    assert_invariant(back)
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(Competitor).where(Competitor.sheet_removed_at.is_not(None))) == 0
        kinds = db.scalars(select(TimelineEvent.kind).where(TimelineEvent.subject_type == "competitor")).all()
        assert kinds.count("sheet_restored") == 1


def test_a_person_in_two_tabs_survives_removal_from_one(sync) -> None:  # type: ignore[no-untyped-def]
    sync()
    pat_row = ["Pat Example", "https://linkedin.com/in/pat-example-123", "Pat Co", "", "", "https://example.test/post", "", "also prospect"]
    pat_only = fx.to_csv([fx.PERSON_HEADER, pat_row])
    sync({**fx.all_tabs_csv(), "Prospects": pat_only})  # 1 of 2 rows vanishes: not over half, so removal runs
    with session_scope() as db:
        pat = db.scalar(select(Prospect).where(Prospect.sheet_status == "Message sent"))
        assert pat.sheet_removed_at is None  # still fed by Outreach
        new = db.scalar(select(SheetSourceRow).where(SheetSourceRow.key == "li:new-person-9"))
        assert new is None
        gone = db.scalars(select(Prospect).where(Prospect.sheet_removed_at.is_not(None))).all()
        assert len(gone) == 1 and gone[0].sheet_removed_at is not None


# --------------------------------------------------------------------------- status mapping
def _by_company(db, name):  # type: ignore[no-untyped-def]
    e = db.scalar(select(Enquiry).where(Enquiry.company == name))
    return e, db.get(Prospect, e.prospect_id), db.scalar(select(Lead).where(Lead.prospect_id == e.prospect_id))


def test_status_rules_end_to_end(sync) -> None:  # type: ignore[no-untyped-def]
    report = sync({"Active Enquiries": matrix_csv()}, tabs=(ACTIVE,))
    assert report.tabs["Active Enquiries"].added == 10
    with session_scope() as db:
        _, p, lead = _by_company(db, "Aurora Co")  # Won
        assert (p.status, lead.stage, lead.opened_by, lead.stage_source) == ("converted", "won", "sheet", "sheet")
        client = db.scalar(select(Client).where(Client.lead_id == lead.id))
        assert client.source == "sheet" and client.status == "active"
        assert lead.closed_at is not None and p.responded_at is not None and p.contacted_at is not None
        _, p, lead = _by_company(db, "Borealis")  # Lost with an e-mail date
        assert (p.status, lead.stage) == ("converted", "lost")
        _, p, lead = _by_company(db, "Cygnus")
        assert lead.stage == "no_response"
        _, p, lead = _by_company(db, "Draco")
        assert lead.stage == "meeting_scheduled" and lead.closed_at is None
        _, p, lead = _by_company(db, "Eridanus")
        assert lead.stage == "proposal_sent" and lead.proposal_sent_on == date(2026, 7, 10)
        _, p, lead = _by_company(db, "Fornax")  # Email Sent
        assert (p.stage, p.status, lead) == ("contacted", "open", None) and p.contacted_estimated
        _, p, lead = _by_company(db, "Gemini")  # plain Closed: a closed prospect, never a false client
        assert (p.status, p.closed_reason, lead) == ("closed", "sheet: Closed", None)
        e, p, lead = _by_company(db, "Hydra")  # unknown: new + logged once
        assert (p.stage, p.status, lead) == ("new", "open", None)
        unknown = db.scalars(select(TimelineEvent).where(TimelineEvent.prospect_id == p.id, TimelineEvent.kind == "sheet_status")).all()
        assert len(unknown) == 1 and unknown[0].data["unknown"] is True
        _, p, _ = _by_company(db, "Indus")
        assert p.stage == "follow_up_2"
        _, p, lead = _by_company(db, "Lyra")
        assert (p.stage, p.status, lead) == ("new", "open", None)
        # stage changes by the sync are stage_change events with actor sheet-sync
        sc = db.scalars(select(TimelineEvent).where(TimelineEvent.kind == "stage_change")).all()
        assert sc and all(e.actor == "sheet-sync" for e in sc)


def test_a_status_edit_moves_the_lead_in_place(sync) -> None:  # type: ignore[no-untyped-def]
    sync({"Active Enquiries": matrix_csv()}, tabs=(ACTIVE,))
    sync({"Active Enquiries": matrix_csv().replace("Meeting Scheduled", "Proposal Sent")}, tabs=(ACTIVE,))
    with session_scope() as db:
        _, p, lead = _by_company(db, "Draco")
        assert lead.stage == "proposal_sent"
        assert db.scalar(select(func.count()).select_from(Lead).where(Lead.prospect_id == p.id)) == 1
        ev = db.scalars(select(TimelineEvent).where(TimelineEvent.kind == "lead_stage", TimelineEvent.prospect_id == p.id)).all()
        assert len(ev) == 1 and ev[0].actor == "sheet-sync"


def test_a_stage_set_in_the_app_is_never_overwritten(sync) -> None:  # type: ignore[no-untyped-def]
    sync({"Active Enquiries": matrix_csv()}, tabs=(ACTIVE,))
    with session_scope() as db:
        _, p, _ = _by_company(db, "Fornax")
        p.stage, p.stage_source = "final_follow_up", "app"  # a human moved it
        pid = p.id
    sync({"Active Enquiries": matrix_csv().replace("Email Sent", "Closed")}, tabs=(ACTIVE,))
    with session_scope() as db:
        p = db.get(Prospect, pid)
        assert (p.stage, p.status, p.stage_source) == ("final_follow_up", "open", "app")
        e = db.scalar(select(Enquiry).where(Enquiry.company == "Fornax"))
        assert e.sheet_status == "Closed"  # the raw status is still refreshed
        ev = db.scalars(select(TimelineEvent).where(TimelineEvent.prospect_id == pid, TimelineEvent.kind == "sheet_status")).all()
        assert len(ev) == 1


# --------------------------------------------------------------------------- adoption
def test_a_sheet_row_adopts_an_existing_discovery_enquiry_and_keeps_its_crm_data(sync) -> None:  # type: ignore[no-untyped-def]
    from sales0.services.dedupe import content_hash

    with session_scope() as db:
        mine = Enquiry(
            source="linkedin_post", content_hash=content_hash("scraped text"), raw_text="scraped text", source_url="https://www.linkedin.com/posts/zz-activity-7000000000000000001-QQ",
            review_status="accepted", triage_status="done", ai_verdict="genuine", ai_reason="a real one", notes="human note", date_added=datetime(2026, 9, 1, tzinfo=UTC), origin="discovery",
        )
        db.add(mine)
        db.flush()
        mine_id = mine.id
    report = sync({"Active Enquiries": fx.active_csv()}, tabs=(ACTIVE,))
    assert report.tabs["Active Enquiries"].added == 7
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(Enquiry).where(Enquiry.origin == "sheet")) == 6  # the first row was adopted
        e = db.get(Enquiry, mine_id)
        assert e.origin == "discovery" and e.raw_text == "scraped text" and e.date_added.replace(tzinfo=UTC) == datetime(2026, 9, 1, tzinfo=UTC)
        assert (e.ai_verdict, e.ai_reason, e.notes) == ("genuine", "a real one", "human note")
        assert e.sheet_status == "Email Sent" and e.company == "Acme Corp"  # sheet-owned columns written
        assert e.prospect_id is not None


def test_seeded_competitors_and_existing_profile_prospects_are_adopted_not_duplicated(sync) -> None:  # type: ignore[no-untyped-def]
    from sales0.db.models import LinkedinProfile

    with session_scope() as db:
        db.add(Competitor(name="Rival One", name_key="rival one", website_key=None))
        prof = LinkedinProfile(slug="in/pat-example-123", kind="person", profile_url="https://www.linkedin.com/in/pat-example-123/", posts_seen=2, relevant_count=0,
                               first_seen_at=NOW, last_seen_at=NOW)
        db.add(prof)
        db.flush()
        mine = Prospect(name="Pat (app)", kind="company_enquiry", linkedin_profile_id=prof.id, stage="new", stage_source="app", status="open", source="enquiry", notes="app note")
        db.add(mine)
        db.flush()
        mine_id = mine.id
    sync()
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(Competitor).where(Competitor.name_key == "rival one")) == 1
        rival = db.scalar(select(Competitor).where(Competitor.name_key == "rival one"))
        assert rival.website_key == "rival.example" and rival.approx_clients == 12
        assert db.scalar(select(func.count()).select_from(Prospect).where(Prospect.linkedin_profile_id == prof.id)) == 1
        p = db.get(Prospect, mine_id)
        assert p.notes == "app note" and p.sheet_status == "Message sent"  # adopted, sheet columns written
        assert p.stage == "contacted"  # untouched app prospect: the sheet may move it


# --------------------------------------------------------------------------- failures
def test_a_failing_row_is_reported_rolled_back_and_retried(sync, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sales0.services.sheetsync import mapping

    real = mapping._apply_competitor

    def boom(ctx, rec, previous):  # type: ignore[no-untyped-def]
        if rec.name == "Rival One":
            ctx.db.add(Company(name="half written", name_key="half written"))
            ctx.db.flush()
            raise RuntimeError("synthetic failure")
        return real(ctx, rec, previous)

    monkeypatch.setattr(mapping, "_apply_competitor", boom)
    report = sync()
    t = report.tabs["Competitors"]
    assert t.added == 2 and [e.code for e in t.errors] == ["apply-failed:RuntimeError"]
    assert_invariant(report)
    with session_scope() as db:
        assert db.scalar(select(Company).where(Company.name_key == "half written")) is None  # rolled back alone
        assert db.scalar(select(func.count()).select_from(Competitor).where(Competitor.name_key == "rival one")) == 0
    monkeypatch.setattr(mapping, "_apply_competitor", real)
    again = sync()
    assert again.tabs["Competitors"].added == 1 and not again.tabs["Competitors"].errors  # retried: hash was not stored
    assert again.total_changes == 1


def test_one_tab_failing_to_fetch_does_not_touch_the_others_or_remove_anything(sync) -> None:  # type: ignore[no-untyped-def]
    sync()
    before = table_counts()
    report = sync({**fx.all_tabs_csv(), "Groups": RuntimeError("down")})
    assert report.tabs["Groups"].fetch_error and not report.ok
    assert report.tabs["Groups"].removed == 0
    assert table_counts() == before


def test_metrics_replace_wholesale_when_the_row_changes(sync) -> None:  # type: ignore[no-untyped-def]
    sync()
    edited = fx.weekly_csv().replace('"1,200"', "1500")
    report = sync({**fx.all_tabs_csv(), "Weekly": edited})
    assert report.tabs["Weekly"].updated == 1
    with session_scope() as db:
        m = db.scalar(select(SheetMetric).where(SheetMetric.metric == "Number of Enquiries added"))
        vals = {(w.week_no, w.occurrence): w.value for w in db.scalars(select(SheetMetricWeek).where(SheetMetricWeek.metric_id == m.id))}
        assert vals[(44, 1)] == 1500 and vals[(43, 1)] == 2 and vals[(43, 2)] == 5


def test_removed_discarded_and_returned_row_is_restored_not_duplicated(sync) -> None:  # type: ignore[no-untyped-def]
    """Keys without a stable post link (row hash / e-mail+summary) must still find their old CRM row."""
    sync()
    base = table_counts()["enquiries"]
    no_discarded = fx.to_csv([fx.DISCARDED_HEADER])
    sync({**fx.all_tabs_csv(), "Discarded Enquiries": no_discarded})
    sync()
    assert table_counts()["enquiries"] == base
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(Enquiry).where(Enquiry.sheet_removed_at.is_not(None))) == 0


def test_apply_record_import_origin_never_overwrites_sheet_fed_rows(sync) -> None:  # type: ignore[no-untyped-def]
    from sales0.services.crm.actor import IMPORT
    from sales0.services.sheetsync import SheetCompetitor, mapping

    sync()
    with session_scope() as db:
        before = db.scalar(select(Competitor.client_names).where(Competitor.name_key == "rival one"))
        rec = SheetCompetitor(key="web:rival.example", website="https://rival.example", website_key="rival.example", name="Rival One", client_names="from csv")
        res = mapping.apply_record(db, rec, origin="import", source=None, actor=IMPORT, now=NOW)
        assert res.action == "unchanged"
        assert db.scalar(select(Competitor.client_names).where(Competitor.name_key == "rival one")) == before
        fresh = SheetCompetitor(key="web:newco.example", website="https://newco.example", website_key="newco.example", name="Newco")
        created = mapping.apply_record(db, fresh, origin="import", source=None, actor=IMPORT, now=NOW)
        assert created.action == "created"
        again = mapping.apply_record(db, fresh, origin="import", source=None, actor=IMPORT, now=NOW)
        assert again.action == "unchanged"
        assert db.scalar(select(func.count()).select_from(SheetSourceRow).where(SheetSourceRow.key == "web:newco.example")) == 0


def test_repository_protocol_surface(db_url) -> None:  # type: ignore[no-untyped-def]
    from sales0.services.sheetsync import SheetRepository

    repo = SqlSheetRepository()
    assert isinstance(repo, SheetRepository)
    assert repo.get_by_key("enquiry", "nope") is None and repo.drain_errors() == [] and repo.list_keys_for_tab("a", "b") == set()
    assert repo.mark_removed("a", "b", [("enquiry", "nope")]) == 0
