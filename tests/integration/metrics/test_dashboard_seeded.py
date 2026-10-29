"""Exact weekly counts over a seeded fixture (>=300 enquiries, 2000+ seen posts, 7 ISO weeks, Asia/Kolkata)."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import event

from sales0.db.engine import get_engine, session_scope
from sales0.services.metrics import compute, report
from sales0.services.metrics.weeks import last_weeks
from tests.support import metrics_seed as S


@pytest.fixture
def seeded(db_url: str) -> dict[str, int]:
    with session_scope() as db:
        info = S.seed(db)
        S.seed_sheet_baseline(db)
    return info


def test_fixture_is_big_enough() -> None:
    assert sum(S.ENQ) + sum(S.MAN) >= 300 and sum(S.SEEN) >= 2000


def test_week_boundaries_are_monday_midnight_in_kolkata_not_utc(db_url: str) -> None:
    from zoneinfo import ZoneInfo

    weeks = last_weeks(S.NOW, ZoneInfo("Asia/Kolkata"), 7)
    assert [w.key for w in weeks] == S.WEEKS
    assert weeks[0].start_utc.isoformat() == "2026-09-13T18:30:00+00:00"  # Sunday 18:30 UTC = Monday 00:00 IST
    assert weeks[-1].end_utc.isoformat() == "2026-11-01T18:30:00+00:00"


def test_every_weekly_counter_matches_the_precomputed_value(seeded: dict[str, int]) -> None:
    from zoneinfo import ZoneInfo

    weeks = last_weeks(S.NOW, ZoneInfo("Asia/Kolkata"), 7)
    with session_scope() as db:
        got = compute.compute_counters(db, weeks)
    for name, expected in S.EXPECTED.items():
        assert got[name] == expected, name
    assert sum(got["scanned"]) == sum(S.SEEN) == 2090


def test_weekly_table_computed_mode_cells_and_sources(seeded: dict[str, int]) -> None:
    with session_scope() as db:
        d = report.build_dashboard(db, now=S.NOW, n_weeks=7, mode="computed")
    rows = {r.key: r for r in d.weekly}
    for key in ("enquiries_added", "enquiries_contacted", "meetings_proposals", "old_prospects"):
        assert [c.value for c in rows[key].cells] == [float(x) for x in S.EXPECTED[key]], key
    # computed wins on overlapping weeks W38/W39 in this mode
    assert [c.source for c in rows["enquiries_added"].cells] == ["computed"] * 7
    # nothing manual entered, no sheet for these rows
    assert [c.source for c in rows["posts_published"].cells] == ["none"] * 7
    assert d.app_start is not None and d.app_start.key == "2026-W38"


def test_merge_rules_never_double_count(seeded: dict[str, int]) -> None:
    with session_scope() as db:
        mx = report.build_dashboard(db, now=S.NOW, n_weeks=12, mode="max")
        sh = report.build_dashboard(db, now=S.NOW, n_weeks=12, mode="sheet")
        cp = report.build_dashboard(db, now=S.NOW, n_weeks=12, mode="computed")
    keys = [w.key for w in mx.weeks]
    assert keys[0] == "2026-W33" and keys[-1] == "2026-W44"
    row = lambda d: {w: c for w, c in zip(keys, next(r for r in d.weekly if r.key == "enquiries_added").cells, strict=True)}  # noqa: E731
    m, s, c = row(mx), row(sh), row(cp)
    # before the app existed: sheet only (same in every mode); no data -> "-"
    for d in (m, s, c):
        assert (d["2026-W36"].value, d["2026-W36"].source) == (7.0, "sheet")
        assert (d["2026-W37"].value, d["2026-W37"].source) == (9.0, "sheet")
        assert d["2026-W35"].value is None and d["2026-W35"].shown == "-"
    # W38: computed 40 > sheet 5; W39: computed 45 < sheet 999
    assert (m["2026-W38"].value, m["2026-W38"].source) == (40.0, "computed")
    assert (m["2026-W39"].value, m["2026-W39"].source) == (999.0, "sheet")
    assert (s["2026-W38"].value, s["2026-W38"].source) == (5.0, "sheet")
    assert (c["2026-W39"].value, c["2026-W39"].source) == (45.0, "computed")
    assert m["2026-W39"].computed == 45.0 and m["2026-W39"].sheet == 999.0  # both exposed, never summed
    assert (m["2026-W40"].value, m["2026-W40"].source) == (52.0, "computed")  # ENQ 50 + MAN 2


def test_manual_figures_beat_the_sheet_and_validate(seeded: dict[str, int]) -> None:
    from sales0.errors import DomainError
    from sales0.services.metrics import store

    with session_scope() as db:
        store.set_manual(db, "posts_published", 2026, 43, 3)
        store.set_manual(db, "posts_published", 2026, 43, 4)  # upsert
        store.set_manual(db, "whatsapp_added", 2026, 42, 9)
        store.set_manual(db, "whatsapp_added", 2026, 42, None)  # clear
        with pytest.raises(DomainError):
            store.set_manual(db, "nonsense", 2026, 43, 1)
        with pytest.raises(DomainError):
            store.set_manual(db, "posts_published", 2026, 54, 1)
        with pytest.raises(DomainError):
            store.set_manual(db, "posts_published", 2026, 43, -1)
    with session_scope() as db:
        d = report.build_dashboard(db, now=S.NOW, n_weeks=7)
    row = next(r for r in d.weekly if r.key == "posts_published")
    assert (row.cells[5].value, row.cells[5].source) == (4.0, "manual")
    assert next(r for r in d.weekly if r.key == "whatsapp_added").cells[4].value is None


def test_secondary_vs_targets_deltas_and_whatsapp_not_enabled(seeded: dict[str, int]) -> None:
    from sales0.services.metrics import store

    with session_scope() as db:
        d = report.build_dashboard(db, now=S.NOW, n_weeks=7, mode="computed", week_key="2026-W43")
        by = {s.key: s for s in d.secondary}
        assert d.selected.key == "2026-W43"
        li = by["linkedin_keyword"]
        assert (li.actual.value, li.target, li.target_origin, li.prev.value) == (52.0, 15.0, "sheet", 48.0)
        assert li.delta == 4.0 and li.pct == round(100 * 52 / 15, 1)
        ms = by["messages_sent"]
        assert (ms.actual.value, ms.target, ms.target_origin) == (15.0 + 3, 30.0, "sheet")  # sheet target 30 beats default 25
        assert by["first_follow_up"].target_origin == "default" and by["first_follow_up"].target == 20.0
        assert by["first_follow_up"].actual.value == 6.0
        assert by["linkedin_outreach"].actual.value == 4.0 and by["linkedin_outreach"].target == 5.0
        assert by["profile_watch"].actual.value == 2.0 and by["manual_paste"].actual.value == 3.0
        wa = by["whatsapp"]
        assert wa.enabled is False and wa.actual is None and wa.target is None
        store.set_target_override(db, "messages_sent", 40)
    with session_scope() as db:
        d = report.build_dashboard(db, now=S.NOW, n_weeks=7, mode="computed", week_key="2026-W43")
        ms = next(s for s in d.secondary if s.key == "messages_sent")
        assert (ms.target, ms.target_origin) == (40.0, "override")


def test_funnel_counts_and_percentages(seeded: dict[str, int]) -> None:
    with session_scope() as db:
        d = report.build_dashboard(db, now=S.NOW, n_weeks=7)
    f = {s.key: s for s in d.funnel.stages}
    relevant = sum(S.EXPECTED["enquiries_added"])
    assert (f["scanned"].count, f["relevant"].count) == (2090, relevant)
    assert f["accepted"].count == sum(S.CONT) + 7 * S.ACCEPTED_UNCONTACTED
    assert f["contacted"].count == sum(S.CONT)
    assert f["replied"].count == sum(S.REPLY) and f["proposal"].count == sum(S.PROPOSAL) and f["won"].count == sum(S.WON)
    assert f["relevant"].pct_prev == round(100 * relevant / 2090, 1)
    assert f["contacted"].pct_prev == round(100 * sum(S.CONT) / f["accepted"].count, 1)
    assert f["won"].pct_first == round(100 * sum(S.WON) / 2090, 2)
    assert [w["relevant"] for w in d.funnel.weekly] == S.EXPECTED["enquiries_added"]
    assert [w["contacted"] for w in d.funnel.weekly] == S.CONT
    assert [w["scanned"] for w in d.funnel.weekly] == S.SEEN


def test_keyword_yield_cost_and_prune_flag(seeded: dict[str, int]) -> None:
    with session_scope() as db:
        d = report.build_dashboard(db, now=S.NOW, n_weeks=7)
    k = {r.text: r for r in d.keywords}
    a = k["kw_alpha"]
    assert (a.scanned, a.relevant, a.runs) == (1050, seeded["alpha_relevant"], 7)
    assert a.relevant_pct == round(100 * a.relevant / 1050, 1) and a.prospects == seeded["alpha_prospects"]
    assert a.cost_micros == 1_050_000 and a.cost_per_relevant_micros == round(1_050_000 / a.relevant)
    assert k["kw_beta"].relevant == seeded["beta_relevant"] and not k["kw_beta"].prune
    dead = k["kw_dead"]
    assert (dead.scanned, dead.relevant, dead.prune, dead.cost_per_relevant_micros) == (140, 0, True, None)
    assert a.prune is False
    cost = d.cost
    assert cost.total_micros == 2_090_000 and cost.relevant == sum(S.EXPECTED["enquiries_added"])
    assert cost.per_relevant_micros == round(2_090_000 / cost.relevant)
    assert cost.weekly_micros == [n * 1000 for n in S.SEEN]


def test_performance_and_bounded_query_count(seeded: dict[str, int]) -> None:
    engine = get_engine()
    statements: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda *a: statements.append(a[2]))
    t0 = time.perf_counter()
    with session_scope() as db:
        report.build_dashboard(db, now=S.NOW, n_weeks=52)
    elapsed = time.perf_counter() - t0
    assert elapsed < 3.0, elapsed
    assert len(statements) < 80, len(statements)  # constant in the data size: no per-row queries


@pytest.mark.sqlite_only
def test_query_plans_use_the_new_indexes(seeded: dict[str, int]) -> None:
    from sqlalchemy import text

    with session_scope() as db:
        for sql, index in (
            ("SELECT first_seen_at FROM seen_posts WHERE first_seen_at >= '2026-09-01' AND first_seen_at < '2026-10-01'", "ix_seen_posts_first_seen_at"),
            ("SELECT subject_id FROM timeline_events WHERE kind = 'research' AND occurred_at >= '2026-09-01'", "ix_timeline_events_kind_occurred"),
            ("SELECT sent_at FROM outreach_messages WHERE sent_at >= '2026-09-01' AND sent_at < '2026-10-01'", "ix_outreach_messages_sent_at"),
            ("SELECT date_added FROM enquiries WHERE date_added >= '2026-09-01' AND date_added < '2026-10-01'", "ix_enquiries_date_added"),
        ):
            plan = " ".join(str(r) for r in db.execute(text("EXPLAIN QUERY PLAN " + sql)))
            assert index in plan, (sql, plan)


def test_fresh_database_renders_empty_state(db_url: str) -> None:
    with session_scope() as db:
        d = report.build_dashboard(db, now=S.NOW, n_weeks=12)
    assert d.empty and d.app_start is None
    assert all(c.value is None for r in d.weekly for c in r.cells)
    assert all(s.count == 0 for s in d.funnel.stages) and d.funnel.stages[1].pct_prev is None
    assert d.cost.per_relevant_micros is None
