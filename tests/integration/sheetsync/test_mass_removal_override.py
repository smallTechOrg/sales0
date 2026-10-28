"""Audit #9: the removal guard has an explicit override - ``sync-sheets --allow-mass-removal`` and the admin-only
"Apply anyway" button on /sync (confirm required, non-admin refused)."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sales0.cli import main, sync_sheets
from sales0.config import get_settings
from sales0.db.engine import session_scope
from sales0.db.models import Enquiry
from sales0.jobs import sheet_sync_job
from sales0.services.sheetsync import StaticSource
from sales0.web.app import create_app
from tests.integration.outreach.conftest import Team, db, team  # noqa: F401 - fixtures
from tests.integration.web.conftest import HX
from tests.unit.sheetsync import fixtures as fx

H = fx.ACTIVE_HEADER


def rows(n: int) -> dict[str, str]:
    body = [fx.active_row(summary=f"Co{i} - t - c", email=f"c{i}@x.example", link=f"https://www.linkedin.com/posts/a-activity-73000000000000{i:05d}-aa") for i in range(n)]
    return {**fx.all_tabs_csv(), "Active Enquiries": fx.to_csv([H, *body])}


def removed() -> int:
    with session_scope() as session:
        return session.scalar(select(func.count()).select_from(Enquiry).where(Enquiry.sheet_removed_at.is_not(None)))


def test_cli_blocks_mass_removal_until_the_flag_is_given(db_url: str, capsys) -> None:  # type: ignore[no-untyped-def]
    s = get_settings()
    assert sync_sheets(s, dry_run=False, tab=None, source=StaticSource(rows(6))) == 0
    sync_sheets(s, dry_run=False, tab=None, source=StaticSource(rows(1)))
    assert removed() == 0 and "REMOVAL-BLOCKED=5-of-6" in capsys.readouterr().out
    assert sync_sheets(s, dry_run=False, tab=None, source=StaticSource(rows(1)), allow_mass_removal=True) == 0
    assert removed() == 5


def test_cli_flag_is_parsed_and_passed_on(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, object] = {}
    import sales0.cli as cli

    monkeypatch.setattr(cli, "_sync_sheets", lambda dry, tab, mem=False, allow=False: seen.update(allow=allow) or 0)
    assert main(["sync-sheets", "--allow-mass-removal"]) == 0 and seen["allow"] is True
    assert main(["sync-sheets"]) == 0 and seen["allow"] is False


@pytest.fixture
def web(team: Team) -> Iterator[TestClient]:  # noqa: F811
    state = {"src": StaticSource(rows(6))}
    sheet_sync_job.set_source_factory(lambda: state["src"])
    with TestClient(create_app()) as c:
        c.state = state  # type: ignore[attr-defined]
        yield c
    sheet_sync_job.set_source_factory(None)


def act_as(web: TestClient, member) -> None:  # noqa: ANN001
    assert web.post("/team/acting", data={"member_id": member.id, "next": "/sync"}, follow_redirects=False).status_code == 303


def wait_idle(web: TestClient) -> dict:
    end = time.monotonic() + 15
    while time.monotonic() < end:
        data = web.get("/api/sync/status", headers={"Accept": "application/json"}).json()["data"]
        if not data["running"]:
            return data
        time.sleep(0.05)
    raise AssertionError("sync never finished")


def test_apply_anyway_is_admin_only_needs_confirmation_and_applies_the_removals(web: TestClient, team: Team) -> None:  # noqa: F811
    act_as(web, team.admin)
    web.post("/sync/run", headers=HX)
    wait_idle(web)
    web.state["src"] = StaticSource(rows(1))  # type: ignore[attr-defined]
    web.post("/sync/run", headers=HX)
    wait_idle(web)
    assert removed() == 0
    html = web.get("/sync").text
    assert 'id="sync-apply-anyway"' in html and "removal blocked" in html

    act_as(web, team.ann)  # a plain member neither sees the button nor can post
    assert 'id="sync-apply-anyway"' not in web.get("/sync").text
    denied = web.post("/sync/run-anyway", data={"confirm": "yes"}, headers={"Accept": "application/json"})
    assert denied.status_code == 403 and removed() == 0

    act_as(web, team.admin)
    unconfirmed = web.post("/sync/run-anyway", data={}, headers={"Accept": "application/json"})
    assert unconfirmed.status_code == 400 and removed() == 0

    ok = web.post("/sync/run-anyway", data={"confirm": "yes"}, headers=HX)
    assert ok.status_code == 200 and "Sync started" in ok.text
    status = wait_idle(web)
    assert status["last_status"] == "ok" and removed() == 5
    assert 'id="sync-apply-anyway"' not in web.get("/sync").text
