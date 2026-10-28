"""Pipeline read models: summary counts and the board."""

from __future__ import annotations

from datetime import date, timedelta

from sales0.services.crm import clients, flags, leads, pipeline, prospects
from tests.integration.crm import helpers as h


def _p(db, company, *, stage=None):
    e = h.enquiry(db, profile_row=h.profile(db, slug=f"in/pl{h._ids.__next__()}"), company=company)
    v = prospects.ensure_for_enquiry(db, e.id)
    if stage:
        prospects.move_stage(db, v.id, stage, now=h.T0)
    return v


def test_empty_summary_has_every_stage_at_zero(db) -> None:
    s = pipeline.summary(db, today=date(2026, 10, 1))
    assert s.prospects_by_stage == {"new": 0, "contacted": 0, "follow_up_1": 0, "follow_up_2": 0, "final_follow_up": 0}
    assert s.leads_by_stage == {"meeting_scheduled": 0, "proposal_sent": 0}
    assert s.closed_recent == {"won": 0, "lost": 0, "no_response": 0}
    assert (s.clients_active, s.clients_legacy, s.due_today, s.overdue) == (0, 0, 0, 0)


def test_summary_counts_everything(db) -> None:
    _p(db, "New Co")
    a = _p(db, "A", stage="contacted")
    _p(db, "B", stage="follow_up_1")
    closed = _p(db, "C")
    prospects.close(db, closed.id, "duplicate")
    prospects.record_response(db, a.id, now=h.T0)
    ld = leads.convert(db, a.id, now=h.T0)
    d = _p(db, "D", stage="contacted")
    prospects.record_response(db, d.id, now=h.T0)
    ld2 = leads.convert(db, d.id, stage="proposal_sent", now=h.T0)
    e = _p(db, "E", stage="contacted")
    prospects.record_response(db, e.id, now=h.T0)
    ld3 = leads.convert(db, e.id, now=h.T0)
    leads.move_stage(db, ld3.id, "won", now=h.T0)
    clients.create_legacy(db, name="Legacy A")
    clients.create_legacy(db, name="Legacy B")
    s = pipeline.summary(db, today=date(2026, 10, 4), now=h.T0)
    assert s.prospects_by_stage["new"] == 1 and s.prospects_by_stage["follow_up_1"] == 1 and s.prospects_by_stage["contacted"] == 0
    assert s.leads_by_stage == {"meeting_scheduled": 1, "proposal_sent": 1}
    assert s.closed_recent["won"] == 1 and s.clients_active == 1 and s.clients_legacy == 2
    # B (follow_up_1 was reached by hand, which created reminders from T0): fu2 (+7d) / final (+14d) are not due on day 3; fu1 was completed
    assert s.due_today == 0 and ld.id and ld2.id


def test_closed_leads_older_than_ninety_days_drop_out_of_closed_recent(db) -> None:
    a = _p(db, "A", stage="contacted")
    prospects.record_response(db, a.id, now=h.T0)
    ld = leads.convert(db, a.id, now=h.T0)
    leads.move_stage(db, ld.id, "lost", now=h.T0)
    assert pipeline.summary(db, now=h.T0 + timedelta(days=89)).closed_recent["lost"] == 1
    assert pipeline.summary(db, now=h.T0 + timedelta(days=91)).closed_recent["lost"] == 0


def test_summary_due_counts_use_the_given_day(db) -> None:
    _p(db, "A", stage="contacted")
    assert pipeline.summary(db, today=h.day(3)).due_today == 1
    assert pipeline.summary(db, today=h.day(5)).overdue == 1


def test_prospect_board_columns_totals_and_card_cap(db) -> None:
    for i in range(3):
        _p(db, f"New {i}")
    _p(db, "Contacted Co", stage="contacted")
    b = pipeline.board(db, per_column=2)
    assert b.kind == "prospects" and [c.stage for c in b.columns] == ["new", "contacted", "follow_up_1", "follow_up_2", "final_follow_up"]
    new_col = b.columns[0]
    assert new_col.label == "New" and new_col.total == 3 and len(new_col.cards) == 2
    assert b.columns[1].total == 1 and b.columns[1].cards[0].name == "Contacted Co" and b.columns[2].cards == []


def test_board_filters_by_query_and_flag_and_ignores_closed(db) -> None:
    a, b_ = _p(db, "Alpha Learning"), _p(db, "Beta Skills")
    flags.set_flag(db, "prospect", b_.id, "repeat_poster", detail={"count": 3})
    c = _p(db, "Gamma")
    prospects.close(db, c.id, "duplicate")
    names = lambda board: [x.name for col in board.columns for x in col.cards]  # noqa: E731
    assert sorted(names(pipeline.board(db))) == ["Alpha Learning", "Beta Skills"]
    assert names(pipeline.board(db, q="alpha")) == ["Alpha Learning"]
    assert names(pipeline.board(db, flag="repeat_poster")) == ["Beta Skills"]
    assert a.id


def test_lead_board_has_five_columns_with_recent_closed_outcomes_only(db) -> None:
    a = _p(db, "A", stage="contacted")
    prospects.record_response(db, a.id, now=h.T0)
    ld = leads.convert(db, a.id, now=h.T0)
    b_ = _p(db, "B", stage="contacted")
    prospects.record_response(db, b_.id, now=h.T0)
    ld2 = leads.convert(db, b_.id, now=h.T0)
    leads.move_stage(db, ld2.id, "won", now=h.T0)
    board = pipeline.board(db, kind="leads", now=h.T0 + timedelta(days=1))
    assert [c.stage for c in board.columns] == ["meeting_scheduled", "proposal_sent", "won", "lost", "no_response"]
    assert board.columns[0].total == 1 and board.columns[0].cards[0].id == ld.id and board.columns[2].total == 1
    old = pipeline.board(db, kind="leads", now=h.T0 + timedelta(days=200))
    assert old.columns[2].total == 0 and old.columns[0].total == 1


def test_board_rejects_an_unknown_kind(db) -> None:
    import pytest

    from sales0.errors import DomainError

    with pytest.raises(DomainError):
        pipeline.board(db, kind="clients")
