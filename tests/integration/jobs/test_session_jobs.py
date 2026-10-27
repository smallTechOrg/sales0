"""Connect / Re-check jobs through the real runner + real headless Chromium on the fixture LinkedIn."""

from __future__ import annotations

import pytest

from sales0.config import Settings
from sales0.db.engine import session_scope
from sales0.errors import DomainError
from sales0.jobs import check_job, connect_job
from sales0.jobs.runner import get_runner
from sales0.scraper.types import ConnectResult, SessionCheckResult
from sales0.services import session as session_service
from tests.integration.services.helpers import set_session_status


def status():
    with session_scope() as db:
        return session_service.session_status(db)


@pytest.mark.browser
def test_start_connect_in_fixture_mode_connects_and_reports_busy_meanwhile(db_url: str, fixture_settings: Settings) -> None:
    with session_scope() as db:
        started = session_service.start_connect(db)
    assert started.status in ("busy", "connected")
    if started.status == "busy":
        assert started.busy_action == "connect" and started.connect_elapsed_s is not None
        with session_scope() as db, pytest.raises(DomainError) as info:
            session_service.start_check(db)
        assert info.value.code == "job_busy"
    assert get_runner().wait(30)
    final = status()
    assert (final.status, final.mode, final.last_error) == ("connected", "fixture", None)
    assert final.last_connected_at is not None and final.last_checked_at is not None


@pytest.mark.browser
def test_recheck_of_a_fresh_profile_reports_not_connected(db_url: str, fixture_settings: Settings) -> None:
    with session_scope() as db:
        session_service.start_check(db)
    assert get_runner().wait(30)
    s = status()
    assert s.status == "not_connected" and s.last_error and "Connect LinkedIn" in s.last_error


@pytest.mark.browser
def test_recheck_after_connect_keeps_connected_and_clears_needs_attention(db_url: str,
                                                                         connected_settings: Settings) -> None:
    set_session_status("needs_attention", "Checkpoint page at /checkpoint/challenge/")
    with session_scope() as db:
        session_service.start_check(db)
    assert get_runner().wait(30)
    s = status()
    assert s.status == "connected" and s.last_error is None


def test_connect_timeout_sets_login_not_completed(db_url: str) -> None:
    connect_job.execute(lambda *_a: ConnectResult(connected=False, reason="timeout", detail=None))
    s = status()
    assert s.status == "not_connected" and s.last_error == "Login not completed — try again"


def test_connect_failure_while_needs_attention_keeps_needs_attention(db_url: str) -> None:
    set_session_status("needs_attention", "Checkpoint page at /checkpoint/challenge/")
    connect_job.execute(lambda *_a: ConnectResult(connected=False, reason="cancelled", detail=None))
    assert status().status == "needs_attention"


def test_connect_crash_is_recorded_not_raised(db_url: str) -> None:
    def boom(*_a: object) -> ConnectResult:
        raise RuntimeError("chromium missing")

    connect_job.execute(boom)
    assert "Connect failed (RuntimeError)" in (status().last_error or "")


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("checkpoint", "needs_attention"), ("captcha", "needs_attention"), ("rate_limited", "needs_attention"),
     ("no_cookie", "not_connected")],
)
def test_recheck_maps_scraper_reasons_to_session_status(db_url: str, reason: str, expected: str) -> None:
    set_session_status("connected")
    check_job.execute(lambda *_a, **_k: SessionCheckResult(logged_in=False, reason=reason, url="http://x/checkpoint/"))  # type: ignore[arg-type]
    assert status().status == expected


def test_recheck_logout_of_a_connected_session_needs_attention(db_url: str) -> None:
    set_session_status("connected")
    check_job.execute(lambda *_a, **_k: SessionCheckResult(logged_in=False, reason="redirected_login", url="http://x/authwall"))
    s = status()
    assert s.status == "needs_attention" and (s.last_error or "").startswith("Logged out of LinkedIn")
