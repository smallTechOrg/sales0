"""QA regressions (synthetic data only): status false positives, guards, idempotent timeline, removal errors."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from sales0.db.engine import session_scope
from sales0.db.models import Enquiry
from sales0.db.models.crm import Client, Lead, Prospect
from sales0.db.models.timeline import TimelineEvent
from sales0.services.sheetsync import StaticSource, SyncEngine
from sales0.services.sheetsync.sql_repository import SqlSheetRepository
from tests.integration.sheetsync.conftest import NOW
from tests.unit.sheetsync import fixtures as fx

ACTIVE, PROSPECTS = fx.TABS[0], fx.TABS[3]
H = fx.ACTIVE_HEADER


def link(n: int) -> str:
    return f"https://www.linkedin.com/posts/a-activity-71000000000000{n:05d}-aa"


def active(rows: list[list[str]]) -> dict[str, str]:
    return {"Active Enquiries": fx.to_csv([H, *rows])}


def row(n: int, **kw: str) -> list[str]:
    return fx.active_row(summary=f"Co{n} - t - c", email=f"c{n}@x.example", link=link(n), **kw)


def lead_of(db, status: str):  # type: ignore[no-untyped-def]
    e = db.scalar(select(Enquiry).where(Enquiry.sheet_status == status))
    p = db.get(Prospect, e.prospect_id)
    return p, db.scalar(select(Lead).where(Lead.prospect_id == p.id))


def test_negated_proposal_statuses_make_no_lead_and_are_and_are_not_unknown(sync) -> None:  # type: ignore[no-untyped-def]
    statuses = ["No proposal yet", "Proposal to be sent", "Not interested in a proposal", "Hot", "warm", "In progress", "ongoing"]
    report = sync(active([row(i, status=s) for i, s in enumerate(statuses)]), tabs=(ACTIVE,))
    assert report.ok
    with session_scope() as db:
        for s in statuses:
            p, lead = lead_of(db, s)
            assert lead is None and p.status == "open" and p.stage == "new" and p.contacted_at is None, s
            assert db.scalar(select(Enquiry.sheet_status).where(Enquiry.sheet_status == s)) == s  # raw text kept
        assert db.scalar(select(Client)) is None
    assert report.tabs["Active Enquiries"].unknown_statuses == 0  # negated phrases and temperature words are understood


def test_proposal_date_opens_a_lead_with_estimated_contact_and_response(sync) -> None:  # type: ignore[no-untyped-def]
    sync(active([row(1, status="New", proposal="10 July"), row(2, status="Closed", proposal="10 July"),
                 row(3, status="No response", sent="10 July")]), tabs=(ACTIVE,))
    with session_scope() as db:
        p, lead = lead_of(db, "New")
        assert lead.stage == "proposal_sent" and p.contacted_at and p.contacted_estimated and p.responded_at
        _, lost = lead_of(db, "Closed")
        assert lost.stage == "lost" and lost.closed_at is not None
        p3, nr = lead_of(db, "No response")
        assert nr.stage == "no_response" and p3.responded_at is None and p3.contacted_at is not None


def test_person_statuses_are_whitelisted_not_substring(sync) -> None:  # type: ignore[no-untyped-def]
    statuses = ["Message sent", "Message not sent", "To be sent", "Consent pending", "Not yet sent", "unsent", "Closed"]
    rows = [[f"P{i}", f"https://www.linkedin.com/in/p{i}-abc", "Co", "", st, "", "", ""] for i, st in enumerate(statuses)]
    report = sync({"Prospects": fx.to_csv([fx.PERSON_HEADER, *rows])}, tabs=(PROSPECTS,))
    with session_scope() as db:
        by = {p.sheet_status: p for p in db.scalars(select(Prospect))}
        assert by["Message sent"].stage == "contacted" and by["Message sent"].contacted_at
        for s in statuses[1:-1]:
            assert by[s].stage == "new" and by[s].contacted_at is None, s
        assert by["Closed"].status == "closed"
    assert report.tabs["Prospects"].unknown_statuses == 0


def test_unknown_status_is_counted_not_an_error(sync) -> None:  # type: ignore[no-untyped-def]
    report = sync(active([row(1, status="Bananas"), row(2, status="Won"), row(3, status="Bananas")]), tabs=(ACTIVE,))
    t = report.tabs["Active Enquiries"]
    assert t.unknown_statuses == 2 and not t.errors and report.ok


# ------------------------------------------------------------------ guards
def _seed(sync, n: int):  # type: ignore[no-untyped-def]
    rows = [row(i) for i in range(n)]
    sync(active(rows), tabs=(ACTIVE,))
    return rows


@pytest.mark.parametrize("n", [1, 2, 5])
def test_header_only_text_never_wipes_a_small_tab(sync, n: int) -> None:  # type: ignore[no-untyped-def]
    _seed(sync, n)
    report = sync(active([]), tabs=(ACTIVE,))
    t = report.tabs["Active Enquiries"]
    assert t.removed == 0 and t.removal_blocked == f"{n}-of-{n}"
    assert any(e.code == "removal-blocked" for e in t.errors)
    with session_scope() as db:
        assert db.scalar(select(Enquiry).where(Enquiry.sheet_removed_at.is_not(None))) is None


def test_more_than_half_of_three_live_rows_vanishing_is_blocked(sync) -> None:  # type: ignore[no-untyped-def]
    rows = _seed(sync, 3)
    t = sync(active(rows[:1]), tabs=(ACTIVE,)).tabs["Active Enquiries"]
    assert t.removed == 0 and t.removal_blocked == "2-of-3"
    t = sync(active(rows[:2]), tabs=(ACTIVE,)).tabs["Active Enquiries"]  # 1 of 3 is fine
    assert t.removed == 1 and t.removal_blocked is None and not t.errors


def test_wrong_tab_header_is_an_error_and_ingests_nothing(sync) -> None:  # type: ignore[no-untyped-def]
    sync({"Prospects": fx.to_csv([fx.PERSON_HEADER, ["Pat", "https://www.linkedin.com/in/pat-abc", "Co", "", "", "", "", ""],
                                  ["Sam", "https://www.linkedin.com/in/sam-abc", "Co", "", "", "", "", ""]])}, tabs=(PROSPECTS,))
    # Google served the first (Active Enquiries) tab for the Prospects gid
    served = fx.to_csv([H, row(1, status="Won")])
    t = sync({"Prospects": served}, tabs=(PROSPECTS,)).tabs["Prospects"]
    assert t.fetch_error == "header-mismatch" and t.added == 0 and t.removed == 0
    # and the reverse: a person header for the enquiries tab
    t = sync({"Active Enquiries": fx.to_csv([fx.PERSON_HEADER, ["a", "b", "c", "", "", "", "", ""]])}, tabs=(ACTIVE,)).tabs[
        "Active Enquiries"
    ]
    assert t.fetch_error and t.added == 0
    with session_scope() as db:
        assert db.scalar(select(Enquiry)) is None
        assert len(db.scalars(select(Prospect)).all()) == 2 and db.scalar(select(Prospect).where(Prospect.sheet_removed_at.is_not(None))) is None


# ------------------------------------------------------------------ restore / removal
def test_restore_writes_no_spurious_created_event(sync) -> None:  # type: ignore[no-untyped-def]
    pat = ["Pat", "https://www.linkedin.com/in/pat-abc", "Co", "", "Message sent", "", "", ""]
    other = ["Sam", "https://www.linkedin.com/in/sam-abc", "Co", "", "", "", "", ""]
    sync({"Prospects": fx.to_csv([fx.PERSON_HEADER, pat, other])}, tabs=(PROSPECTS,))
    sync({"Prospects": fx.to_csv([fx.PERSON_HEADER, other])}, tabs=(PROSPECTS,))
    with session_scope() as db:
        before = {e.id for e in db.scalars(select(TimelineEvent))}
    sync({"Prospects": fx.to_csv([fx.PERSON_HEADER, pat, other])}, tabs=(PROSPECTS,))
    with session_scope() as db:
        new = [e.kind for e in db.scalars(select(TimelineEvent).order_by(TimelineEvent.id)) if e.id not in before]
    assert "sheet_restored" in new and "created" not in new


def test_mark_removed_failure_surfaces_as_an_error_entry(sync, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    rows = _seed(sync, 4)

    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("disk")

    monkeypatch.setattr(SqlSheetRepository, "_soft_remove", staticmethod(boom))
    t = sync(active(rows[:3]), tabs=(ACTIVE,)).tabs["Active Enquiries"]
    assert t.removed == 0
    assert [e.code for e in t.errors] == ["remove-failed:RuntimeError"]
    assert t.errors[0].row > 0


def test_unchanged_second_sync_writes_no_timeline_events(sync) -> None:  # type: ignore[no-untyped-def]
    rows = [row(1, status="Email Sent", sent="10 July"), row(2, status="Bananas"), row(3, status="Won")]
    sync(active(rows), tabs=(ACTIVE,))
    with session_scope() as db:
        n = len(db.scalars(select(TimelineEvent)).all())
    report = SyncEngine(StaticSource(active(rows)), SqlSheetRepository(clock=lambda: datetime(2026, 10, 29, tzinfo=UTC)), (ACTIVE,),
                        today=lambda: fx.TODAY, now=lambda: NOW).run()
    assert report.total_changes == 0
    with session_scope() as db:
        assert len(db.scalars(select(TimelineEvent)).all()) == n
