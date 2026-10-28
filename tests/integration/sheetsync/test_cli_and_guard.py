"""CLI (sync-sheets, crm-counts) against the CRM and the read-only guarantee for the new code path."""

from __future__ import annotations

import pathlib
import re

import httpx
import pytest

import sales0.services.sheetsync as pkg
from sales0.cli import crm_counts, main, sync_sheets
from sales0.config import Settings, get_settings
from sales0.services.sheetsync import SheetFetcher, StaticSource, SyncEngine, make_client, tabs_from_settings
from sales0.services.sheetsync.sql_repository import SqlSheetRepository
from tests.unit.sheetsync import fixtures as fx


def test_cli_default_repository_is_the_crm(db_url: str, capsys) -> None:  # type: ignore[no-untyped-def]
    code = sync_sheets(get_settings(), dry_run=False, tab=None, source=StaticSource(fx.all_tabs_csv()))
    out = capsys.readouterr().out
    assert code == 0 and "APPLY" in out and "in-memory" not in out
    assert "Active Enquiries: rows=8 added=7" in out
    assert crm_counts() == 0
    counts = dict(p.split("=") for p in capsys.readouterr().out.split())
    assert int(counts["enquiries"]) == 9 and int(counts["sheet_metrics"]) == 7 and int(counts["channel_groups"]) == 4
    # second run: nothing to do
    sync_sheets(get_settings(), dry_run=False, tab=None, source=StaticSource(fx.all_tabs_csv()))
    out2 = capsys.readouterr().out
    assert "added=0 updated=0" in out2 and "total: changes=0" in out2


def test_cli_dry_run_and_tab_and_memory(db_url: str, capsys) -> None:  # type: ignore[no-untyped-def]
    src = StaticSource(fx.all_tabs_csv())
    assert sync_sheets(get_settings(), dry_run=True, tab="Groups", source=src) == 0
    assert "DRY-RUN" in capsys.readouterr().out
    crm_counts()
    assert "enquiries=0" in capsys.readouterr().out and "groups" not in ""  # nothing written
    assert sync_sheets(get_settings(), dry_run=False, tab="Competitors", source=src, memory=True) == 0
    assert "--memory" in capsys.readouterr().out
    crm_counts()
    assert "competitors=" in capsys.readouterr().out


@pytest.mark.unmigrated
def test_cli_unmigrated_database_exits_3(capsys) -> None:  # type: ignore[no-untyped-def]
    src = StaticSource(fx.all_tabs_csv())
    assert sync_sheets(Settings(_env_file=None), dry_run=False, tab=None, source=src) == 3
    assert "alembic upgrade head" in capsys.readouterr().err
    assert crm_counts() == 3
    # --memory needs no database at all
    assert sync_sheets(Settings(_env_file=None), dry_run=True, tab=None, source=src, memory=True) == 0


def test_cli_entry_points_parse(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict = {}
    monkeypatch.setattr("sales0.cli.crm_counts", lambda: seen.setdefault("counts", 0))
    assert main(["crm-counts"]) == 0 and "counts" in seen


# ----------------------------------------------------------------------------- nothing can write to Google
def test_a_full_crm_sync_over_the_real_fetcher_issues_only_gets_to_allowed_hosts(db_url: str) -> None:
    log: list[httpx.Request] = []
    csv_by_gid = {t.gid: fx.all_tabs_csv()[t.name] for t in fx.TABS}

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        return httpx.Response(200, text=csv_by_gid[request.url.params["gid"]], headers={"content-type": "text/csv"})

    fetcher = SheetFetcher(make_client(inner=httpx.MockTransport(handler)), retries=0)
    engine = SyncEngine(fetcher, SqlSheetRepository(), fx.TABS, today=lambda: fx.TODAY)
    report = engine.run()
    assert report.ok and report.tabs["Active Enquiries"].added == 7
    assert len(log) == len(fx.TABS)
    assert {r.method for r in log} == {"GET"}
    assert {r.url.host for r in log} == {"docs.google.com"}
    assert all(not r.content for r in log) and all("authorization" not in r.headers for r in log)


def test_the_crm_code_path_cannot_issue_a_write_even_if_asked(db_url: str) -> None:
    """Whatever the repository / mapping does, the only HTTP client is the read-only one."""
    from sales0.services.sheetsync import SheetWriteBlocked

    client = make_client(inner=httpx.MockTransport(lambda r: httpx.Response(200)))
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(SheetWriteBlocked):
            client.request(method, "https://sheets.googleapis.com/v4/spreadsheets/x/values/A1:append", content=b"x")
    settings = Settings(_env_file=None)
    assert tabs_from_settings(settings)  # building the tab list needs no credentials


def test_new_modules_hold_no_http_client_or_google_write_primitives() -> None:
    root = pathlib.Path(pkg.__file__).parent
    new = ["sql_repository.py", "mapping.py", "status_map.py", "metrics.py"]
    banned = [
        r"httpx", r"requests", r"urllib", r"aiohttp", r"socket", r"\.post\(", r"\.put\(", r"\.patch\(", r"\.delete\(",
        r"googleapiclient", r"gspread", r"oauth", r"service_account", r"spreadsheets\.values", r"batchUpdate", r"Authorization",
    ]
    for name in new:
        code = "\n".join(line for line in (root / name).read_text().splitlines() if not line.lstrip().startswith("#"))
        for pattern in banned:
            assert not re.search(pattern, code, re.IGNORECASE), (name, pattern)
    # the web route and the job only reach Google through the read-only fetcher
    base = root.parents[1]
    for rel in ("web/routes/sync.py", "jobs/sheet_sync_job.py"):
        code = (base / rel).read_text()
        assert "httpx" not in code and not re.search(r"\.(post|put|patch|delete)\(", code.replace("@router.post(", ""))
