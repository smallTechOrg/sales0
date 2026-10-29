"""Audit-data regressions (synthetic rows only): #1 edit-adoption, #4 frozen years, #7 changed-only writes,
#10 duplicate keys, #9 mass-removal override, #16 settings.tz."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import func, select

from sales0.db.engine import session_scope
from sales0.db.models import Enquiry
from sales0.db.models.crm import Flag, Prospect
from sales0.db.models.timeline import TimelineEvent
from sales0.services.crm.actor import Actor
from sales0.services.sheetsync import StaticSource, SyncEngine
from sales0.services.sheetsync.sql_repository import SqlSheetRepository
from tests.integration.sheetsync.conftest import NOW
from tests.unit.sheetsync import fixtures as fx

ACTIVE = fx.TABS[0]
H = fx.ACTIVE_HEADER


def active(rows: list[list[str]]) -> dict[str, str]:
    return {"Active Enquiries": fx.to_csv([H, *rows])}


def linkless(n: int, **kw: str) -> list[str]:
    """A row WITHOUT a post link / Full Enquiry: its key is the e-mail+summary hash."""
    base = {"summary": f"Co{n} - Topic - City | Online", "email": f"c{n}@x.example", "company": f"Co{n}", "person": f"Person {n}", "added": "12 July"}
    base.update(kw)
    return fx.active_row(**base)


def filler(n: int) -> list[str]:
    return fx.active_row(summary=f"Fill{n} - t - c", email=f"f{n}@x.example", link=f"https://www.linkedin.com/posts/a-activity-72000000000000{n:05d}-aa")


def counts() -> tuple[int, int]:
    with session_scope() as db:
        return db.scalar(select(func.count()).select_from(Enquiry)), db.scalar(select(func.count()).select_from(Prospect))


# ------------------------------------------------------------------------------------------------ #1
def test_editing_summary_or_email_of_a_linkless_row_keeps_the_same_enquiry_and_prospect(sync) -> None:  # type: ignore[no-untyped-def]
    sync(active([linkless(1), filler(1), filler(2)]), tabs=(ACTIVE,))
    with session_scope() as db:
        e = db.scalar(select(Enquiry).where(Enquiry.company == "Co1"))
        eid, pid = e.id, e.prospect_id
    assert counts() == (3, 3)

    sync(active([linkless(1, summary="Co1 - Edited topic - City | Online"), filler(1), filler(2)]), tabs=(ACTIVE,))
    sync(active([linkless(1, summary="Co1 - Edited topic - City | Online", email="new.addr@x.example"), filler(1), filler(2)]), tabs=(ACTIVE,))

    assert counts() == (3, 3)
    with session_scope() as db:
        e = db.get(Enquiry, eid)
        assert e.sheet_removed_at is None and e.prospect_id == pid
        assert e.requirement_summary.startswith("Co1 - Edited topic") and e.email == "new.addr@x.example"
        assert db.get(Prospect, pid).status == "open"
        assert db.scalar(select(func.count()).select_from(Enquiry).where(Enquiry.sheet_removed_at.is_not(None))) == 0


def test_adoption_by_previous_row_number_when_company_and_contact_are_blank(sync) -> None:  # type: ignore[no-untyped-def]
    row = fx.active_row(summary="Solo - Thing - Town | Online", email="solo@x.example", added="12 July")
    sync(active([row, filler(1), filler(2)]), tabs=(ACTIVE,))
    row2 = fx.active_row(summary="Solo - Thing changed - Town | Online", email="solo@x.example", added="12 July")
    sync(active([row2, filler(1), filler(2)]), tabs=(ACTIVE,))
    assert counts() == (3, 3)


def test_two_different_rows_are_not_merged_by_adoption(sync) -> None:  # type: ignore[no-untyped-def]
    """Same company+contact+date but BOTH rows are still in the sheet: two enquiries."""
    a = linkless(1, summary="Co1 - Topic A - City | Online", email="a@x.example", company="Same", person="Pat", added="12 July")
    b = linkless(2, summary="Co1 - Topic B - City | Online", email="b@x.example", company="Same", person="Pat", added="12 July")
    sync(active([a, b, filler(1), filler(2)]), tabs=(ACTIVE,))
    assert counts() == (4, 3)  # two enquiries; the company model keeps ONE outreach record for the company "Same"


def test_removed_row_closes_an_untouched_prospect_and_flags_one_with_human_history(sync) -> None:  # type: ignore[no-untyped-def]
    from sales0.services.crm import timeline

    rows = [linkless(1), linkless(2), filler(1), filler(2)]
    sync(active(rows), tabs=(ACTIVE,))
    with session_scope() as db:
        p2 = db.scalar(select(Enquiry).where(Enquiry.company == "Co2")).prospect_id
        timeline.add_event(db, subject_type="prospect", subject_id=p2, kind="note", body="Called, wants a quote", actor=Actor("Asha"))
    sync(active([filler(1), filler(2), filler(3)]), tabs=(ACTIVE,))  # Co1 and Co2 rows both gone (2 of 4 = 50%)

    with session_scope() as db:
        p1 = db.get(Prospect, db.scalar(select(Enquiry).where(Enquiry.company == "Co1")).prospect_id)
        p2 = db.get(Prospect, p2)
        assert db.scalar(select(Enquiry).where(Enquiry.company == "Co1")).sheet_removed_at is not None
        assert p1.status == "closed" and p1.closed_reason == "sheet row removed"
        assert p2.status == "open"
        kinds = {f.kind for f in db.scalars(select(Flag).where(Flag.entity_type == "prospect", Flag.entity_id == p2.id, Flag.cleared_at.is_(None)))}
        assert "source_row_removed" in kinds
        assert not db.scalars(select(Flag).where(Flag.entity_id == p1.id, Flag.kind == "source_row_removed")).first()


def test_a_returning_row_reopens_the_prospect_the_sync_closed_and_clears_the_flag(sync) -> None:  # type: ignore[no-untyped-def]
    rows = [linkless(1), filler(1), filler(2), filler(3)]
    sync(active(rows), tabs=(ACTIVE,))
    sync(active(rows[1:]), tabs=(ACTIVE,))
    with session_scope() as db:
        assert db.scalar(select(Prospect).where(Prospect.status == "closed")) is not None
    sync(active(rows), tabs=(ACTIVE,))
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(Prospect).where(Prospect.status == "closed")) == 0


# ------------------------------------------------------------------------------------------------ #4
def _engine(texts: dict[str, str], today: date, repo: SqlSheetRepository, **kw) -> SyncEngine:  # type: ignore[no-untyped-def]
    return SyncEngine(StaticSource(texts), repo, (ACTIVE,), today=lambda: today, now=lambda: NOW, **kw)


def test_inferred_year_is_frozen_while_day_and_month_stay(db_url) -> None:  # type: ignore[no-untyped-def]
    repo = SqlSheetRepository(clock=lambda: NOW)
    r1 = linkless(1, posted="09 July", added="12 July", notes="v1")
    _engine(active([r1, filler(1), filler(2)]), date(2026, 10, 28), repo).run()
    # a year later the row is EDITED (notes only): the day/month are the same, so the year must not move
    r2 = linkless(1, posted="09 July", added="12 July", notes="v2")
    rep = _engine(active([r2, filler(1), filler(2)]), date(2027, 10, 28), repo).run()
    assert rep.tabs["Active Enquiries"].updated == 1
    with session_scope() as db:
        e = db.scalar(select(Enquiry).where(Enquiry.company == "Co1"))
        assert e.date_posted == date(2026, 7, 9) and e.date_added.astimezone().year == 2026
        assert e.sheet_notes == "v2"
    # changing the day/month re-infers from today
    r3 = linkless(1, posted="09 July", added="15 July", notes="v2")
    _engine(active([r3, filler(1), filler(2)]), date(2027, 10, 28), repo).run()
    with session_scope() as db:
        e = db.scalar(select(Enquiry).where(Enquiry.company == "Co1"))
        assert e.date_added.astimezone().month == 7 and e.date_added.astimezone().year == 2027


def test_feb_29_year_is_frozen_too(db_url) -> None:  # type: ignore[no-untyped-def]
    repo = SqlSheetRepository(clock=lambda: NOW)
    _engine(active([linkless(1, posted="29 Feb", added="29 Feb", notes="a"), filler(1), filler(2)]), date(2028, 6, 1), repo).run()
    _engine(active([linkless(1, posted="29 Feb", added="29 Feb", notes="b"), filler(1), filler(2)]), date(2032, 6, 1), repo).run()
    with session_scope() as db:
        assert db.scalar(select(Enquiry).where(Enquiry.company == "Co1")).date_posted == date(2028, 2, 29)


# ------------------------------------------------------------------------------------------------ #7
def test_in_app_edit_of_a_sheet_owned_column_survives_an_unrelated_sheet_change(sync) -> None:  # type: ignore[no-untyped-def]
    sync(active([linkless(1, priority="High", notes="n1"), filler(1), filler(2)]), tabs=(ACTIVE,))
    with session_scope() as db:
        e = db.scalar(select(Enquiry).where(Enquiry.company == "Co1"))
        e.priority = "low"  # an in-app edit of a sheet-owned column
        eid = e.id
    sync(active([linkless(1, priority="High", notes="n2"), filler(1), filler(2)]), tabs=(ACTIVE,))
    with session_scope() as db:
        e = db.get(Enquiry, eid)
        assert e.priority == "low" and e.sheet_notes == "n2"
        ev = db.scalars(select(TimelineEvent).where(TimelineEvent.enquiry_id == eid, TimelineEvent.kind == "sheet_updated").order_by(TimelineEvent.id.desc())).first()
        assert ev.data["fields"] == ["sheet_notes"]
        assert ev.data["changes"]["sheet_notes"] == {"old": "n1", "new": "n2"}
    # a change of the SAME column in the sheet still wins
    sync(active([linkless(1, priority="Medium", notes="n2"), filler(1), filler(2)]), tabs=(ACTIVE,))
    with session_scope() as db:
        assert db.get(Enquiry, eid).priority == "medium"


# ------------------------------------------------------------------------------------------------ #10
def _dups(order: tuple[int, int], *, shared_post: bool) -> list[list[str]]:
    """Two rows with the SAME natural key but different content (a shared post link, or the same e-mail+summary)."""
    if shared_post:
        link = "https://www.linkedin.com/posts/d-activity-7300000000000000001-dd"
        rows = [fx.active_row(summary="Dup - First - Pune | Online", email="d1@x.example", link=link, notes="first"),
                fx.active_row(summary="Dup - First - Pune | Online", email="d2@x.example", link=link, notes="second")]
    else:
        rows = [fx.active_row(summary="Dup - Same - Pune | Online", email="d1@x.example", notes="first"),
                fx.active_row(summary="Dup - Same - Pune | Online", email="d1@x.example", notes="second")]
    return [rows[i] for i in order]


def _snapshot() -> dict[str, tuple[int, bool]]:
    with session_scope() as db:
        return {e.sheet_notes: (e.id, e.sheet_removed_at is not None) for e in db.scalars(select(Enquiry).where(Enquiry.requirement_summary.like("Dup -%")))}


def test_shared_post_duplicates_are_one_enquiry_whose_content_does_not_flip(sync) -> None:  # type: ignore[no-untyped-def]
    pad = [filler(1), filler(2), filler(3)]
    sync(active([*_dups((0, 1), shared_post=True), *pad]), tabs=(ACTIVE,))
    before = _snapshot()
    assert set(before) == {"first"} and before["first"][1] is False  # one enquiry: the first row wins, the second is an extra source

    rep = sync(active([*_dups((1, 0), shared_post=True), *pad]), tabs=(ACTIVE,))  # rows swapped
    assert _snapshot() == before and rep.total_changes == 0

    sync(active([*_dups((1, 0), shared_post=True), *pad[:2]]), tabs=(ACTIVE,))  # an unrelated row deleted
    assert _snapshot() == before

    sync(active([_dups((0, 1), shared_post=True)[0], *pad]), tabs=(ACTIVE,))  # the extra row deleted
    assert _snapshot() == before

    sync(active([_dups((0, 1), shared_post=True)[1], *pad]), tabs=(ACTIVE,))  # the winner deleted: the other row carries the enquiry
    after = _snapshot()
    assert set(after) == {"second"} and after["second"] == (before["first"][0], False)


def test_same_key_rows_with_own_enquiries_keep_their_identity_when_reordered_or_one_is_deleted(sync) -> None:  # type: ignore[no-untyped-def]
    pad = [filler(1), filler(2), filler(3)]
    sync(active([*_dups((0, 1), shared_post=False), *pad]), tabs=(ACTIVE,))
    before = _snapshot()
    assert set(before) == {"first", "second"} and before["first"][0] != before["second"][0]

    rep = sync(active([*_dups((1, 0), shared_post=False), *pad]), tabs=(ACTIVE,))
    assert _snapshot() == before and rep.total_changes == 0

    sync(active([*_dups((1, 0), shared_post=False), *pad[:2]]), tabs=(ACTIVE,))
    assert _snapshot() == before

    sync(active([_dups((0, 1), shared_post=False)[1], *pad]), tabs=(ACTIVE,))  # the first row deleted
    after = _snapshot()
    assert after["second"] == before["second"]  # untouched: same enquiry, still live, no content moved
    assert after["first"] == (before["first"][0], True)


# ------------------------------------------------------------------------------------------------ #9
def test_mass_removal_is_blocked_and_the_override_applies_it(sync) -> None:  # type: ignore[no-untyped-def]
    rows = [filler(1), filler(2), filler(3), filler(4)]
    sync(active(rows), tabs=(ACTIVE,))
    blocked = sync(active(rows[:1]), tabs=(ACTIVE,))
    assert blocked.tabs["Active Enquiries"].removal_blocked == "3-of-4" and blocked.tabs["Active Enquiries"].removed == 0
    repo = SqlSheetRepository(clock=lambda: NOW)
    rep = _engine(active(rows[:1]), fx.TODAY, repo, allow_mass_removal=True).run()
    assert rep.tabs["Active Enquiries"].removed == 3 and rep.tabs["Active Enquiries"].removal_blocked is None
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(Enquiry).where(Enquiry.sheet_removed_at.is_not(None))) == 3


# ------------------------------------------------------------------------------------------------ #16
def test_engine_default_today_uses_the_configured_timezone(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from zoneinfo import ZoneInfo

    from sales0.config import get_settings
    from sales0.services.sheetsync import InMemorySheetRepository

    # 23:30 UTC on 27 Oct is already 28 Oct in Kolkata
    late = datetime(2026, 10, 27, 23, 30, tzinfo=UTC)
    monkeypatch.setattr(type(get_settings()), "tz", property(lambda self: ZoneInfo("Asia/Kolkata")), raising=False)
    eng = SyncEngine(StaticSource({}), InMemorySheetRepository(), (), now=lambda: late)
    assert eng._today() == date(2026, 10, 28)  # noqa: SLF001


# ------------------------------------------------------------------------------------------------ #5 / #13
def test_a_company_page_in_the_people_tab_is_a_company_profile_not_a_person(sync) -> None:  # type: ignore[no-untyped-def]
    from sales0.db.models import LinkedinProfile
    from sales0.db.models.contacts import Contact
    from sales0.db.models.sheetdata import SheetSource

    person_tab = fx.TABS[3]
    rows = [
        ["Pat", "https://www.linkedin.com/in/Pat-Example%2D123/?trk=1", "Pat Co", "", "", "", "", ""],
        ["Acme page", "linkedin.com/company/Acme-Corp", "Acme", "", "", "", "", ""],
        ["Filler", "https://www.linkedin.com/in/filler-1", "F", "", "", "", "", ""],
    ]
    sync({"Prospects": fx.to_csv([fx.PERSON_HEADER, *rows])}, tabs=(person_tab,))
    with session_scope() as db:
        profiles = {p.slug: p.kind for p in db.scalars(select(LinkedinProfile))}
        assert profiles["company/acme-corp"] == "company" and "in/acme-corp" not in profiles
        assert profiles["in/pat-example-123"] == "person"
        keys = set(db.scalars(select(SheetSource.key).where(SheetSource.entity == "prospect")))
        assert {"li:pat-example-123", "li:company/acme-corp"} <= keys
        assert db.scalar(select(Contact.id).where(Contact.linkedin_slug.like("%acme%"))) is None  # a page is not a person's profile
    # an unchanged re-sync is a no-op (the new key is stable)
    again = sync({"Prospects": fx.to_csv([fx.PERSON_HEADER, *rows])}, tabs=(person_tab,))
    assert again.total_changes == 0


def test_client_started_on_is_the_local_date_not_the_utc_date(sync) -> None:  # type: ignore[no-untyped-def]
    from sales0.db.models.crm import Client

    late_utc = datetime(2026, 10, 27, 23, 30, tzinfo=UTC)  # already 28 Oct in Asia/Kolkata (the default zone)
    sync(active([fx.active_row(summary="Won - t - c", email="w@x.example", status="Won", added="12 July")]), tabs=(ACTIVE,), now=late_utc)
    with session_scope() as db:
        assert db.scalar(select(Client.started_on)) == date(2026, 10, 28)
