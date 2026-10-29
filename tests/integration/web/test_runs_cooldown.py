"""/runs renders the Phase 1.1 safety cool-down: after a rate-limit / captcha / checkpoint /
logout stop, Run now is disabled with the ``safety_cooldown`` reason (local HH:MM), POST
/runs is refused (toast for htmx, 429 envelope for JSON), the stopped run's outcome carries
"Next run allowed after HH:MM", and once the cool-down is over Run now is enabled again.
Real app + real SQLite file; stop times are seeded on a whole minute relative to now."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from html import unescape

import pytest
from fastapi.testclient import TestClient

from sales0.clock import utcnow
from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import ScrapeRun
from sales0.web.context import cooldown_clock
from tests.integration.web.conftest import HX, make_run, set_session

REASON_RE = re.compile(r"LinkedIn asked us to slow down — next run allowed after (\d\d:\d\d(?: tomorrow)?)")


@pytest.fixture(autouse=True)
def _production_cooldowns(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The root conftest turns the cool-down off; these tests use the shipped 60/15."""
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "60")
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN", "15")
    reset_settings()


def _panel(html: str) -> str:
    start = html.index('<section id="run-panel"')
    return html[start : html.index("</section>", start)]


def _run_now_disabled(html: str) -> bool:
    tag = re.search(r'<button id="run-now"[^>]*>', html)
    assert tag, "no Run now button"
    return re.search(r"\sdisabled[\s>]", tag.group(0)) is not None


def _reason(panel: str) -> str:
    m = re.search(r'<span id="run-now-reason"[^>]*>(.*?)</span>', panel, re.S)
    assert m, "no disabled reason rendered"
    return m.group(1)


def _ready(client: TestClient) -> None:
    """A green session with one keyword — everything but the cool-down allows a run."""
    set_session("connected")
    resp = client.post("/setup/keywords", data={"text": "ai workshop"}, headers=HX)
    assert resp.status_code == 200 and "ai workshop" in unescape(resp.text), resp.text


def _stop_at(minutes_ago: int) -> datetime:
    return utcnow().replace(second=0, microsecond=0) - timedelta(minutes=minutes_ago)


def _stopped(kind: str, minutes_ago: int) -> int:
    at = _stop_at(minutes_ago)
    return make_run("aborted", abort_reason=kind, started_at=at - timedelta(minutes=1), finished_at=at,
                    keywords=[(f"{kind}-kw", "error")])


def _expected_clock(minutes_ago: int, cooldown_min: int) -> str:
    return cooldown_clock(_stop_at(minutes_ago) + timedelta(minutes=cooldown_min))


def test_rate_limit_stop_disables_run_now_with_the_cooldown_reason_and_refuses_post(client: TestClient) -> None:
    _ready(client)
    _stopped("rate_limited", minutes_ago=10)

    panel = unescape(_panel(client.get("/runs").text))
    assert _run_now_disabled(panel)
    assert 'data-reason-code="safety_cooldown"' in panel
    m = REASON_RE.search(_reason(panel))
    assert m, panel
    assert m.group(1) == _expected_clock(10, 60)
    # The time is said once in the panel (the disabled reason), not again in the outcome…
    assert "Last run stopped: LinkedIn rate limit detected" in panel
    assert panel.count(_expected_clock(10, 60)) == 1, panel
    # …while the run detail (no Run now button there) carries the cool-down line.
    run_id = int(re.search(r'data-last-run-id="(\d+)"', panel).group(1))  # type: ignore[union-attr]
    detail = unescape(client.get(f"/runs/{run_id}").text)
    assert f"Next run allowed after {_expected_clock(10, 60)}" in detail

    refused = client.post("/runs", headers=HX)
    assert refused.headers.get("HX-Retarget") == "#toasts" and "toast--error" in refused.text
    assert "LinkedIn asked us to slow down" in unescape(refused.text)
    with session_scope() as db:
        assert db.query(ScrapeRun).count() == 1  # nothing queued


def test_json_run_now_during_cooldown_is_a_429_envelope(client: TestClient) -> None:
    _ready(client)
    _stopped("captcha", minutes_ago=1)
    resp = client.post("/runs", headers={"Accept": "application/json"})
    assert resp.status_code == 429, resp.text
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "safety_cooldown", body
    assert REASON_RE.search(body["error"]["message"]), body


@pytest.mark.parametrize("kind", ["checkpoint", "logout"])
def test_checkpoint_and_logout_cooldown_is_the_shorter_window(client: TestClient, kind: str) -> None:
    _ready(client)
    run_id = _stopped(kind, minutes_ago=10)
    panel = unescape(_panel(client.get("/runs").text))
    assert 'data-reason-code="safety_cooldown"' in panel
    m = REASON_RE.search(_reason(panel))
    assert m and m.group(1) == _expected_clock(10, 15), panel
    detail = unescape(client.get(f"/runs/{run_id}").text)
    assert f"Next run allowed after {_expected_clock(10, 15)}" in detail


def test_run_now_is_enabled_again_after_the_cooldown(client: TestClient) -> None:
    _ready(client)
    _stopped("rate_limited", minutes_ago=61)
    panel = unescape(_panel(client.get("/runs").text))
    assert not _run_now_disabled(panel) and 'id="run-now-reason"' not in panel
    assert "Next run allowed after" not in panel  # the outcome no longer claims a cool-down


def test_needs_attention_wins_over_the_cooldown_and_setup_says_when_runs_resume(client: TestClient) -> None:
    _ready(client)
    _stopped("rate_limited", minutes_ago=5)
    set_session("needs_attention", last_error="LinkedIn rate-limited the session at /search/results/content/")
    panel = unescape(_panel(client.get("/runs").text))
    assert 'data-reason-code="needs_attention"' in panel, panel
    # The reason shown is needs_attention, so the outcome carries the cool-down time.
    assert f"Next run allowed after {_expected_clock(5, 60)}" in panel

    card = unescape(client.get("/setup").text).split('id="linkedin-card"', 1)[1].split("</section>", 1)[0]
    assert "Press Connect LinkedIn — the sales0 Chrome window opens on LinkedIn so you can resolve it there." in card
    assert f"After that, the next run is allowed after {_expected_clock(5, 60)}." in card


def test_cooldown_settings_of_zero_turn_it_off(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "0")
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN", "0")
    reset_settings()
    _ready(client)
    _stopped("rate_limited", minutes_ago=1)
    panel = unescape(_panel(client.get("/runs").text))
    assert not _run_now_disabled(panel) and "Next run allowed after" not in panel


def test_cooldown_clock_reads_tomorrow_on_a_later_local_day() -> None:
    now = datetime(2026, 10, 27, 18, 0, tzinfo=None)  # naive = UTC in the app's clock helpers
    from sales0.clock import ensure_utc

    now = ensure_utc(now)  # 23:30 in Asia/Kolkata
    assert cooldown_clock(now + timedelta(minutes=15), now=now) == "23:45"
    assert cooldown_clock(now + timedelta(minutes=60), now=now) == "00:30 tomorrow"
    assert cooldown_clock(now + timedelta(minutes=15, seconds=1), now=now) == "23:46"  # never early
