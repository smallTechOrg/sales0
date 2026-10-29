"""E4: prospect priority - derived from the linked enquiries, human-editable (manual is never overwritten),
filter + sort in the pipeline services, backfill-equivalent derivation, sheet-driven enquiry priority."""

from __future__ import annotations

from sales0.db.models import Enquiry, Prospect
from sales0.services import events, inbox
from sales0.services.crm import clients, leads, pipeline, priority, prospects
from tests.integration.crm import helpers as h


def _p(db, company, prio, **kw):  # type: ignore[no-untyped-def]
    e = h.enquiry(db, profile_row=h.profile(db, slug=f"in/pr{h._ids.__next__()}"), company=company, priority=prio, **kw)
    v = prospects.ensure_for_enquiry(db, e.id)
    return v, e


def test_prospect_priority_is_the_highest_of_its_enquiries(db, installed) -> None:
    prof = h.profile(db, slug="in/multi")
    a = h.enquiry(db, profile_row=prof, company="Multi", priority="low")
    v = prospects.ensure_for_enquiry(db, a.id)
    assert db.get(Prospect, v.id).priority == "low" and db.get(Prospect, v.id).priority_source == "derived"
    b = h.enquiry(db, profile_row=prof, company="Multi", priority="high")
    prospects.ensure_for_enquiry(db, b.id)
    assert db.get(Prospect, v.id).priority == "high"
    # the enquiry's priority edited in the Inbox flows to the prospect
    inbox.update_fields(db, b.id, priority="medium")
    assert db.get(Prospect, v.id).priority == "medium"
    # a discarded enquiry stops counting
    b.review_status = "discarded"
    events.publish(db, events.EnquiryDecided(enquiry_id=b.id, decision="discard", previous_review_status="accepted"))
    assert db.get(Prospect, v.id).priority == "low"


def test_manual_priority_is_never_overwritten_until_set_back_to_automatic(db, installed) -> None:
    v, e = _p(db, "Manual Co", "low")
    priority.set_priority(db, v.id, "high")
    row = db.get(Prospect, v.id)
    assert (row.priority, row.priority_source) == ("high", "manual")
    inbox.update_fields(db, e.id, priority="medium")  # enquiry changes
    e.priority = "low"
    priority.refresh_for_enquiry(db, e.id)  # and a sheet-style refresh
    assert db.get(Prospect, v.id).priority == "high"
    priority.set_priority(db, v.id, "")
    row = db.get(Prospect, v.id)
    assert (row.priority, row.priority_source) == ("low", "derived")
    detail = prospects.get_prospect(db, v.id)
    assert any("Priority" in (t.body or "") for t in detail.timeline)


def test_invalid_priority_is_rejected(db, installed) -> None:
    v, _ = _p(db, "Bad Co", "low")
    try:
        priority.set_priority(db, v.id, "urgent")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", "") == "priority_invalid"
    else:
        raise AssertionError("expected priority_invalid")


def test_filter_and_sort_in_board_list_leads_and_clients(db, installed) -> None:
    hi, _ = _p(db, "Hi Co", "high")
    md, _ = _p(db, "Mid Co", "medium")
    lo, _ = _p(db, "Low Co", "low")
    none, _ = _p(db, "None Co", None)
    names = lambda page: [i.name for i in page.items]  # noqa: E731
    assert names(prospects.list_prospects(db, priority_filter="high")) == ["Hi Co"]
    assert names(prospects.list_prospects(db, priority_filter="none")) == ["None Co"]
    assert names(prospects.list_prospects(db, sort="priority")) == ["Hi Co", "Mid Co", "Low Co", "None Co"]
    # combinable with the stage filter
    prospects.move_stage(db, md.id, "contacted", now=h.T0)
    assert names(prospects.list_prospects(db, priority_filter="medium", stage="contacted")) == ["Mid Co"]
    assert prospects.list_prospects(db, priority_filter="medium", stage="new").total == 0
    board = pipeline.board(db, priority_filter="high")
    assert sum(c.total for c in board.columns) == 1
    sorted_board = pipeline.board(db, sort="priority")
    new_col = next(c for c in sorted_board.columns if c.stage == "new")
    assert [c.name for c in new_col.cards] == ["Hi Co", "Low Co", "None Co"]
    assert all(c.priority == p for c, p in zip(new_col.cards, ["high", "low", None], strict=True))
    # leads + clients follow the prospect's priority
    prospects.move_stage(db, hi.id, "contacted", now=h.T0)
    prospects.record_response(db, hi.id, now=h.T0)
    ld = leads.convert(db, hi.id, now=h.T0)
    assert [x.prospect_name for x in leads.list_leads(db, priority_filter="high").items] == ["Hi Co"]
    assert leads.list_leads(db, priority_filter="low").total == 0
    assert leads.get_lead(db, ld.id).priority == "high"
    leads.move_stage(db, ld.id, "won")
    cl = clients.list_clients(db, priority_filter="high")
    assert [c.name for c in cl.items] == ["Hi Co"] and cl.items[0].priority == "high"
    assert clients.list_clients(db, priority_filter="low").total == 0


def test_enquiry_category_filter_in_the_pipeline(db, installed) -> None:
    a, ea = _p(db, "Plain Co", "high")
    b, eb = _p(db, "Partner Co", "high")
    eb.category = "channel_partner"
    db.flush()
    assert [i.name for i in prospects.list_prospects(db, category="channel_partner").items] == ["Partner Co"]
    assert [i.name for i in prospects.list_prospects(db, category="relevant_prospect").items] == ["Plain Co"]
    view = prospects.get_prospect(db, b.id)
    assert view.categories == ["channel_partner"]
    assert db.get(Enquiry, eb.id).category == "channel_partner" and a.id != b.id
