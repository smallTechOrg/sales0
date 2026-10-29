"""Phase 1.1 read models: SessionStatusView browser line + cool-down, RunView.layout_warnings
(computed on read), and the Phase 1.1 needs-attention wording. Real SQLite file at head."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sales0.db.engine import session_scope
from sales0.db.models import ScrapeRun, ScrapeRunKeyword
from sales0.scraper import browser as browser_mod
from sales0.scraper.browser import BrowserChoice
from sales0.scraper.errors import BrowserUnavailable
from sales0.services import keywords, runs
from sales0.services import session as session_service
from tests.integration.services.helpers import set_session_status

T0 = datetime(2026, 10, 27, 8, 0, tzinfo=UTC)
CHROME_MISSING = ("Google Chrome is not installed — install it from https://www.google.com/chrome/ "
                  "or set SALES0_LINKEDIN_BROWSER_CHANNEL=auto")


def status():
    with session_scope() as db:
        return session_service.session_status(db)


def test_session_view_reports_the_resolved_browser_label(db_url: str) -> None:
    # The test harness pins channel=chromium (deterministic on any machine).
    view = status()
    assert view.browser_label == "Built-in browser (Chromium)"
    assert view.browser_note is None and view.browser_channel == "chromium"
    assert view.cooldown_until is None


def test_session_view_shows_the_fallback_note(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    note = "Google Chrome not found — using the built-in browser"
    monkeypatch.setattr(browser_mod, "resolve_browser",
                        lambda _s: BrowserChoice(channel="chromium", version="140.0", label="Built-in browser (Chromium)",
                                                 note=note))
    view = status()
    assert (view.browser_label, view.browser_note) == ("Built-in browser (Chromium)", note)


def test_session_view_on_browser_unavailable_says_chrome_not_installed(db_url: str,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_s: object) -> BrowserChoice:
        raise BrowserUnavailable(CHROME_MISSING)

    monkeypatch.setattr(browser_mod, "resolve_browser", missing)
    view = status()
    assert view.browser_label == "Google Chrome (not installed)"
    assert view.browser_note == CHROME_MISSING
    assert view.browser_channel is None


def test_session_view_serialises_without_any_cookie_value(db_url: str) -> None:
    data = status().model_dump(mode="json")
    assert {"browser_label", "browser_note", "cooldown_until"} <= set(data)
    assert "li_at" not in str(data) and "cookie" not in str(data).lower()


def test_needs_attention_reason_uses_the_phase_1_1_wording(db_url: str) -> None:
    set_session_status("needs_attention", "Checkpoint page at /checkpoint/challenge/.")
    with session_scope() as db:
        e = runs.run_eligibility(db)
    assert e.reason_code == "needs_attention"
    assert e.reason == ("LinkedIn needs attention: Checkpoint page at /checkpoint/challenge/. Press Connect LinkedIn "
                        "in Setup — the sales0 Chrome window opens on LinkedIn so you can resolve it.")


def _run_with(statuses: list[str]) -> int:
    with session_scope() as db:
        run = ScrapeRun(trigger="manual", mode="fixture", status="succeeded", started_at=T0, finished_at=T0)
        db.add(run)
        db.flush()
        db.add_all(ScrapeRunKeyword(run_id=run.id, keyword_text=f"kw {i}", status=s) for i, s in enumerate(statuses))
        return run.id


def test_layout_warnings_counts_warning_keyword_rows_in_every_read_model(db_url: str) -> None:
    clean = _run_with(["done", "error", "skipped"])
    drifted = _run_with(["warning", "done", "warning"])
    with session_scope() as db:
        assert runs.get_run(db, clean).run.layout_warnings == 0
        assert runs.get_run(db, drifted).run.layout_warnings == 2
        assert {r.id: r.layout_warnings for r in runs.list_runs(db)} == {clean: 0, drifted: 2}
        payload = runs.get_run(db, drifted).model_dump(mode="json")
        assert payload["run"]["layout_warnings"] == 2


def test_active_run_carries_layout_warnings_so_far(db_url: str) -> None:
    rid = _run_with(["warning", "running"])
    with session_scope() as db:
        run = db.get(ScrapeRun, rid)
        assert run is not None
        run.status, run.finished_at = "running", None
    with session_scope() as db:
        active = runs.active_run(db)
        assert active is not None and (active.id, active.layout_warnings) == (rid, 1)


def test_a_run_with_no_keyword_rows_has_zero_layout_warnings(db_url: str) -> None:
    rid = _run_with([])
    with session_scope() as db:
        assert runs.get_run(db, rid).run.layout_warnings == 0
        keywords.add_keyword(db, "ai workshop")
