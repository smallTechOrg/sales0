"""Cost report (exact figures from seeded llm_calls) and the monthly spend cap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, LlmCall
from sales0.services import costs_report
from sales0.services.costs_report import SpendCapReached, build_report, ensure_within_spend_cap, spend_cap_status
from sales0.services.triage import triage_enquiry
from tests.integration.services.helpers import make_enquiry
from tests.support.fake_llm import FakeLLMClient, triage_answer

# Asia/Kolkata (UTC+5:30): 2026-10-28 12:00 UTC is 17:30 local on the 28th.
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def _call(db, when: datetime, cost: int, *, feature: str = "triage", model: str = "claude-haiku-5-5", enquiry_id=None, status="ok"):  # noqa: ANN001, ANN202
    db.add(LlmCall(feature=feature, model=model, enquiry_id=enquiry_id, input_tokens=1000, output_tokens=100,
                   cost_usd_micros=cost, status=status, created_at=when))


@pytest.fixture
def seeded(db_url: str) -> None:
    e1, e2 = make_enquiry("one", n=1), make_enquiry("two", n=2)
    with session_scope() as db:
        _call(db, NOW - timedelta(hours=1), 1000, enquiry_id=e1)  # today
        _call(db, NOW - timedelta(hours=2), 3000, enquiry_id=e2)  # today
        _call(db, NOW - timedelta(hours=3), 500, feature="drafting")  # today
        _call(db, NOW - timedelta(days=2), 2000, feature="research", model="claude-other")  # 26th
        _call(db, NOW - timedelta(days=30), 7000)  # previous month (28 Sep)
        # 2026-10-27 19:00 UTC = 00:30 local on the 28th: belongs to the LOCAL day 28, not the 27th.
        _call(db, datetime(2026, 10, 27, 19, 0, tzinfo=UTC), 100)
        _call(db, NOW - timedelta(hours=4), 0, status="error")


def test_report_totals_buckets_and_unit_costs(seeded: None) -> None:
    with session_scope() as db:
        r = build_report(db, NOW)
    assert (r.today_micros, r.today_calls) == (1000 + 3000 + 500 + 100, 5)
    assert (r.month_micros, r.month_calls) == (1000 + 3000 + 500 + 2000 + 100, 6)
    by_feature = {b.key: (b.cost_micros, b.calls) for b in r.by_feature}
    assert by_feature == {"triage": (4100, 4), "research": (2000, 1), "drafting": (500, 1)}
    assert {b.key: b.cost_micros for b in r.by_model} == {"claude-haiku-5-5": 4600, "claude-other": 2000}
    daily = {b.key: (b.cost_micros, b.calls) for b in r.daily}
    assert len(daily) == 30 and daily["2026-10-28"] == (4600, 5) and daily["2026-10-26"] == (2000, 1)
    assert daily["2026-10-27"] == (0, 0)
    months = {b.key: b.cost_micros for b in r.monthly}
    assert months["2026-10"] == 6600 and months["2026-09"] == 7000 and len(months) == 6
    assert r.triaged_enquiries == 2 and r.cost_per_enquiry_micros == 4100 // 2
    assert r.drafts == 0 and r.cost_per_draft_micros is None
    assert r.errors_month == 1


def test_cap_states_off_ok_warn_exceeded(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    month = 6600  # micros this month
    with session_scope() as db:
        assert spend_cap_status(db, NOW).state == "off"
        ensure_within_spend_cap(db, NOW)  # no cap: never raises
    for cap_usd, state in ((month * 2 / 1e6, "ok"), (month * 1.1 / 1e6, "warn"), (month / 1e6, "exceeded"), (month / 2e6, "exceeded")):
        monkeypatch.setenv("SALES0_MONTHLY_SPEND_CAP_USD", repr(cap_usd))
        reset_settings()
        with session_scope() as db:
            st = spend_cap_status(db, NOW)
            assert st.state == state, (cap_usd, st)
            if state == "exceeded":
                with pytest.raises(SpendCapReached) as exc:
                    ensure_within_spend_cap(db, NOW)
                assert exc.value.code == "spend_cap_reached" and exc.value.http_status == 429
            else:
                ensure_within_spend_cap(db, NOW)
            assert (st.message is None) == (state in ("ok", "off"))


def test_previous_month_spend_does_not_count_against_this_months_cap(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_MONTHLY_SPEND_CAP_USD", "0.0067")
    reset_settings()
    with session_scope() as db:
        assert spend_cap_status(db, NOW).state == "warn"  # 6600 of 6700 micros; Sep's 7000 is excluded
        # next month starts clean
        assert spend_cap_status(db, datetime(2026, 11, 1, 1, 0, tzinfo=UTC)).month_micros == 0


def test_triage_over_cap_makes_no_call_leaves_pending_and_no_llm_row(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    eid = make_enquiry("Acme wants a GenAI workshop for 40 managers in Pune")
    with session_scope() as db:
        _call(db, datetime.now(UTC), 5000)
    monkeypatch.setenv("SALES0_MONTHLY_SPEND_CAP_USD", "0.001")
    reset_settings()
    fake = FakeLLMClient([triage_answer()])
    with session_scope() as db, pytest.raises(SpendCapReached):
        triage_enquiry(db, eid, fake)
    assert fake.requests == []
    with session_scope() as db:
        e = db.get(Enquiry, eid)
        assert e is not None and e.triage_status == "pending" and e.ai_verdict is None
        assert db.scalar(select(func.count(LlmCall.id))) == 1  # only the seeded row
    # raising the cap lets the retry through
    monkeypatch.setenv("SALES0_MONTHLY_SPEND_CAP_USD", "1")
    reset_settings()
    with session_scope() as db:
        assert triage_enquiry(db, eid, fake).status == "done"


def test_fatal_tuple_includes_cap() -> None:
    from sales0.services.triage import FATAL_LLM_ERRORS

    assert SpendCapReached in FATAL_LLM_ERRORS and costs_report.SpendCapReached is SpendCapReached
