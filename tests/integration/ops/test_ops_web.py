"""/costs, /schedule and the spend-cap banner through the real app (TestClient)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import Digest, LlmCall, ScheduledJob
from sales0.jobs.scheduler import get_scheduler
from sales0.web.app import create_app
from tests.integration.web.conftest import HX  # noqa: F401  (re-exported fixtures below)
from tests.integration.web.conftest import client  # noqa: F401


def _seed_calls() -> None:
    now = datetime.now(UTC)
    with session_scope() as db:
        db.add(LlmCall(feature="triage", model="claude-haiku-5-5", cost_usd_micros=250_000, input_tokens=10, output_tokens=5, status="ok", created_at=now))
        db.add(LlmCall(feature="drafting", model="claude-haiku-5-5", cost_usd_micros=100_000, status="ok", created_at=now))


def test_costs_page_shows_totals_features_models(client: TestClient) -> None:  # noqa: F811
    _seed_calls()
    html = client.get("/costs").text
    assert 'id="cap-line"' in html and "No cap is set" in html
    assert re.search(r'id="today-cost">\$0\.3500', html), html[html.index("totals-table"):][:600]
    assert 'data-feature="triage"' in html and 'data-feature="drafting"' in html
    assert 'data-model="claude-haiku-5-5"' in html
    assert 'id="spend-cap-banner"' not in html


def test_cap_banner_warns_then_hard_stop_message_on_every_page(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    _seed_calls()  # $0.35 this month
    monkeypatch.setenv("SALES0_MONTHLY_SPEND_CAP_USD", "0.40")
    reset_settings()
    for path in ("/inbox", "/costs", "/schedule"):
        html = client.get(path).text
        assert 'id="spend-cap-banner"' in html and "88% of the monthly cap" in html
    monkeypatch.setenv("SALES0_MONTHLY_SPEND_CAP_USD", "0.30")
    reset_settings()
    html = client.get("/costs").text
    assert "Monthly AI spend cap reached" in html and 'data-state="exceeded"' in html
    assert "new enquiries stay pending" in client.get("/runs").text


def test_schedule_page_lists_jobs_toggle_and_run_now(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv("SALES0_DIGEST_AI_SUMMARY", "false")
    reset_settings()
    html = client.get("/schedule").text
    for name in ("discovery_run", "daily_digest", "sheet_sync", "purge"):
        assert f'id="job-{name}"' in html
    assert 'id="job-discovery_run" data-enabled="0"' in html and 'id="job-daily_digest" data-enabled="1"' in html
    assert "scheduler-off" in html  # tests run with the thread disabled

    r = client.post("/schedule/discovery_run/enable", data={"enabled": "1"}, follow_redirects=False)
    assert r.status_code == 303
    html = client.get("/schedule").text
    assert 'id="job-discovery_run" data-enabled="1"' in html and "Plan for" in html
    with session_scope() as db:
        assert db.query(ScheduledJob).filter_by(name="discovery_run").one().enabled is True

    client.post("/schedule/daily_digest/run", follow_redirects=False)
    html = client.get("/schedule").text
    assert "Built the digest for" in html
    with session_scope() as db:
        assert db.query(Digest).count() == 1
    assert client.post("/schedule/nope/run", follow_redirects=False).status_code == 404


def test_run_now_of_discovery_explains_why_it_was_skipped(client: TestClient) -> None:  # noqa: F811
    client.post("/schedule/discovery_run/run", follow_redirects=False)
    html = client.get("/schedule").text
    assert re.search(r'data-status="skipped"', html) and "Connect LinkedIn first" in html  # not connected in a fresh DB


def test_lifespan_starts_and_stops_the_scheduler_thread(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCHEDULER_ENABLED", "true")
    reset_settings()
    with TestClient(create_app()):
        assert get_scheduler().running
        sched = get_scheduler()
    assert not sched.running
