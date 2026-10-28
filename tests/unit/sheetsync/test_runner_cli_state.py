from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sales0.cli import main as cli_main
from sales0.cli import sync_sheets
from sales0.config import Settings
from sales0.db.models.sheet_sync import SheetSyncState
from sales0.services.sheetsync import (
    InlineExecutor,
    InMemoryStateStore,
    SheetSyncRunner,
    SqlStateStore,
    StaticSource,
    SyncReport,
    InMemorySheetRepository,
    default_tabs,
    tabs_from_settings,
)
from tests.unit.sheetsync import fixtures as fx

T0 = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


def ok_report() -> SyncReport:
    return SyncReport(dry_run=False, started_at=T0, finished_at=T0)


class NoJitter:
    def uniform(self, a: float, b: float) -> float:
        return 0.0

    def random(self) -> float:
        return 0.0


def make_runner(job, clock, **kw):
    return SheetSyncRunner(job, interval_min=kw.pop("interval_min", 30), now=clock, executor=InlineExecutor(),
                           rng=NoJitter(), first_run_delay_s=20, **kw)  # type: ignore[arg-type]


def test_first_run_shortly_after_boot_then_every_interval() -> None:
    clock, runs = Clock(), []
    r = make_runner(lambda: (runs.append(1), ok_report())[1], clock)
    assert r.tick() is False and runs == []
    clock.advance(seconds=21)
    assert r.tick() is True and len(runs) == 1
    assert r.tick() is False  # next is 30 min away
    clock.advance(minutes=29)
    assert r.tick() is False
    clock.advance(minutes=2)
    assert r.tick() is True and len(runs) == 2
    st = r.status()
    assert st.runs == 2 and st.last_ok is True and st.enabled and not st.running and st.next_run_at is not None


def test_zero_disables_scheduling_but_manual_trigger_works() -> None:
    clock, runs = Clock(), []
    r = make_runner(lambda: (runs.append(1), ok_report())[1], clock, interval_min=0)
    clock.advance(days=3)
    assert r.tick() is False and runs == [] and r.status().enabled is False
    assert r.trigger_now() is True and len(runs) == 1


def test_never_overlaps_itself() -> None:
    clock = Clock()
    gate, started = threading.Event(), threading.Event()
    calls = []

    def slow() -> SyncReport:
        calls.append(1)
        started.set()
        gate.wait(5)
        return ok_report()

    from concurrent.futures import ThreadPoolExecutor

    ex = ThreadPoolExecutor(max_workers=2)
    r = SheetSyncRunner(slow, interval_min=1, now=clock, executor=ex, rng=NoJitter(), first_run_delay_s=0)  # type: ignore[arg-type]
    clock.advance(seconds=1)
    assert r.tick() is True
    assert started.wait(5)
    clock.advance(minutes=5)  # way past due while still running
    assert r.tick() is False and r.trigger_now() is False
    assert r.status().running is True
    gate.set()
    ex.shutdown(wait=True)
    assert len(calls) == 1 and r.status().running is False


def test_survives_errors_and_failed_tabs_and_exposes_status() -> None:
    clock = Clock()
    n = {"i": 0}

    def flaky() -> SyncReport:
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("network down")
        rep = ok_report()
        if n["i"] == 2:
            from sales0.services.sheetsync import TabReport

            rep.tabs["X"] = TabReport(tab="X", kind="groups", fetch_error="http-500")
        return rep

    r = make_runner(flaky, clock)
    clock.advance(seconds=21)
    r.tick()
    st = r.status()
    assert st.last_ok is False and st.last_error.startswith("RuntimeError") and st.consecutive_failures == 1
    clock.advance(minutes=31)
    r.tick()
    st = r.status()
    assert st.last_ok is False and st.consecutive_failures == 2 and "tabs failed" in st.last_error
    clock.advance(minutes=31)
    r.tick()
    st = r.status()
    assert st.last_ok is True and st.consecutive_failures == 0 and st.last_error is None and st.last_report["ok"]


def test_jitter_stays_within_bounds_and_is_applied() -> None:
    import random

    clock = Clock()
    r = SheetSyncRunner(ok_report, interval_min=30, jitter=0.2, now=clock, executor=InlineExecutor(),
                        rng=random.Random(5), first_run_delay_s=0)
    gaps = set()
    for _ in range(20):
        clock.advance(minutes=40)
        r.tick()
        gaps.add((r.status().next_run_at - clock()).total_seconds())
    assert len(gaps) > 5 and all(24 * 60 <= g <= 36 * 60 for g in gaps)


def test_restart_resumes_from_persisted_last_finish() -> None:
    clock = Clock()
    state = InMemoryStateStore()
    state.last_finished = T0 - timedelta(minutes=10)
    runs = []
    r = make_runner(lambda: (runs.append(1), ok_report())[1], clock, state=state)
    clock.advance(seconds=30)
    assert r.tick() is False  # last sync was 10 min ago: not due for 20 more
    clock.advance(minutes=20)
    assert r.tick() is True and len(runs) == 1 and len(state.runs) == 1 and state.runs[0]["status"] == "ok"


def test_sql_state_store_round_trip_and_migration_matches_model(tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 's.db'}")
    SheetSyncState.__table__.create(eng)
    store = SqlStateStore(sessionmaker(eng))
    assert store.load_last_finished() is None and store.load() is None
    rep = ok_report()
    rep.finished_at = T0 + timedelta(seconds=5)
    store.save_run(T0, rep, None)
    store.save_run(T0, None, "RuntimeError: x")
    got = store.load()
    assert got["runs_total"] == 2 and got["last_status"] == "failed" and got["last_error"] == "RuntimeError: x"
    assert store.load_last_finished() is not None


def test_migration_0003_creates_exactly_the_model_table(tmp_path) -> None:
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from sales0.db.base import metadata
    from sales0.db.migrate_check import alembic_config

    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0003_sheet_sync_state")
    eng = create_engine(url)
    with eng.connect() as conn:
        diff = [d for d in compare_metadata(MigrationContext.configure(conn), metadata) if "sheet_sync_state" in repr(d)]
    assert diff == []
    command.downgrade(cfg, "0002_phase1_discovery")


# ------------------------------------------------------------------ settings + CLI
def test_settings_defaults_and_tab_overrides(monkeypatch) -> None:
    monkeypatch.delenv("SALES0_SHEET_SYNC_MINUTES", raising=False)
    s = Settings(_env_file=None)
    assert s.sheet_sync_minutes == 30
    tabs = tabs_from_settings(s)
    assert tabs == default_tabs() and tabs[0].gid == "0" and tabs[0].name == "Active Enquiries"
    s2 = Settings(_env_file=None, sheet_sync_minutes=0, sheet_client_sourcing_id="X",
                  sheet_tabs_json='[{"name":"Leads","kind":"enquiries","gid":5}]')
    assert s2.sheet_sync_minutes == 0
    (t,) = tabs_from_settings(s2)
    assert (t.name, t.sheet_id, t.gid) == ("Leads", "X", "5")


def test_cli_dry_run_prints_counts_only(capsys) -> None:
    s = Settings(_env_file=None)
    code = sync_sheets(s, dry_run=True, tab=None, source=StaticSource(fx.all_tabs_csv()))
    out = capsys.readouterr().out
    assert code == 0 and "DRY-RUN" in out
    assert "Active Enquiries: rows=8 added=7" in out and "Weekly:" in out
    for secret in ("acme", "@", "linkedin", "Pat Example"):
        assert secret.lower() not in out.lower()


def test_cli_single_tab_unknown_tab_and_failures(capsys) -> None:
    s = Settings(_env_file=None)
    src = StaticSource({**fx.all_tabs_csv(), "Groups": RuntimeError("x")})
    assert sync_sheets(s, dry_run=True, tab="groups", source=src) == 1
    assert "FETCH-ERROR" in capsys.readouterr().out
    assert sync_sheets(s, dry_run=True, tab="nope", source=src) == 2
    assert sync_sheets(s, dry_run=True, tab="competitors", source=src) == 0


def test_cli_entry_point_registers_subcommand(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr("sales0.cli._sync_sheets", lambda dry, tab: seen.update(dry=dry, tab=tab) or 0)
    assert cli_main(["sync-sheets", "--dry-run", "--tab", "Groups"]) == 0
    assert seen == {"dry": True, "tab": "Groups"}
    assert cli_main(["sync-sheets"]) == 0 and seen == {"dry": False, "tab": None}


def test_cli_uses_registered_repository_when_present(capsys) -> None:
    from sales0.services.sheetsync import register_repository_factory

    repo = InMemorySheetRepository()
    register_repository_factory(lambda: repo)
    try:
        sync_sheets(Settings(_env_file=None), dry_run=False, tab="Groups", source=StaticSource(fx.all_tabs_csv()))
        assert any(k[0] == "group" for k in repo.records)
        assert "in-memory" not in capsys.readouterr().out
    finally:
        register_repository_factory(None)
