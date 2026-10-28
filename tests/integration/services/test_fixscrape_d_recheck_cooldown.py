"""Fixscrape D #4: a Re-check-detected stop starts the cool-down; Re-check (and cookie validation) are refused
during it; Connect stays allowed."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from sales0.clock import utcnow
from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.errors import DomainError
from sales0.jobs import check_job
from sales0.jobs.runner import get_runner, reset_runner
from sales0.scraper.types import SessionCheckResult
from sales0.services import linkedin_accounts, runs
from sales0.services import session as session_service
from tests.integration.services.helpers import set_session_status


@pytest.fixture(autouse=True)
def _cooldowns(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "60")
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN", "15")
    reset_settings()
    set_session_status("connected")


def detect(reason: str) -> None:
    check_job.execute(lambda *_a, **_k: SessionCheckResult(logged_in=False, reason=reason, url="http://x/checkpoint/challenge/"))


def cooldown():
    with session_scope() as db:
        return runs.safety_cooldown_until(db)


@pytest.mark.parametrize(("reason", "minutes"), [("captcha", 60), ("rate_limited", 60), ("checkpoint", 15)])
def test_check_detected_stop_records_a_timestamp_and_starts_the_cooldown(reason: str, minutes: int) -> None:
    before = utcnow()
    detect(reason)
    with session_scope() as db:
        row = linkedin_accounts.get_account(db, linkedin_accounts.default_account_id(db))
        assert row.safety_state == reason and row.last_stop_at is not None
        assert row.cooldown_until is not None  # snapshot mirrored on the account row
    until = cooldown()
    assert until is not None
    assert timedelta(minutes=minutes - 1) < until - before <= timedelta(minutes=minutes, seconds=5)
    # a green Re-check later does not clear it
    with session_scope() as db:
        assert runs.run_eligibility(db).reason_code in ("needs_attention", "safety_cooldown")


def test_logout_detected_by_a_check_uses_the_short_cooldown() -> None:
    detect("redirected_login")  # was connected -> logout
    assert cooldown() is not None
    with session_scope() as db:
        assert linkedin_accounts.get_account(db, linkedin_accounts.default_account_id(db)).safety_state == "logout"


def test_recheck_is_refused_during_the_cooldown_with_the_minutes_left() -> None:
    detect("captcha")
    with session_scope() as db, pytest.raises(DomainError) as info:
        session_service.start_check(db)
    err = info.value
    assert (err.code, err.http_status) == ("safety_cooldown", 429)
    assert "Re-check is paused for" in err.message and "minutes" in err.message and "Connect LinkedIn" in err.message
    assert get_runner().busy() is None  # nothing was submitted


def test_recheck_is_allowed_again_after_the_cooldown_and_without_one(monkeypatch: pytest.MonkeyPatch) -> None:
    with session_scope() as db:
        session_service.assert_recheck_allowed(db)  # no stop at all
    detect("checkpoint")
    with session_scope() as db:
        row = linkedin_accounts.get_account(db, linkedin_accounts.default_account_id(db))
        row.last_stop_at = utcnow() - timedelta(minutes=16)
    with session_scope() as db:
        session_service.assert_recheck_allowed(db)


def test_connect_stays_allowed_during_the_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    detect("captcha")
    submitted: list[str] = []
    monkeypatch.setattr(get_runner(), "submit", lambda kind, fn: submitted.append(kind))
    with session_scope() as db:
        session_service.start_connect(db)
    assert submitted == ["connect"]


def test_cooldown_is_per_account() -> None:
    detect("captcha")
    with session_scope() as db:
        other = linkedin_accounts.add_account(db, "Second", backend="folder").id
    with session_scope() as db:
        assert runs.safety_cooldown_until(db, account_id=other) is None
        session_service.assert_recheck_allowed(db, other)


def test_zero_cooldown_setting_never_blocks_recheck(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "0")
    reset_settings()
    detect("captcha")
    with session_scope() as db:
        session_service.assert_recheck_allowed(db)


def test_setup_check_route_returns_429_and_cookie_paste_is_stored_but_not_validated(db_url: str) -> None:
    from sales0.web.app import create_app

    detect("captcha")
    with TestClient(create_app()) as c:
        r = c.post("/setup/linkedin/check", headers={"HX-Request": "true"})
        assert "Re-check is paused" in r.text  # htmx gets the message as a toast
        r = c.post("/setup/linkedin/check")  # a plain POST gets the error page with the real status
        assert r.status_code == 429 and "Re-check is paused" in r.text
        with session_scope() as db:
            acc = linkedin_accounts.get_account(db, linkedin_accounts.default_account_id(db))
            acc.backend = "db"
            acc_id = acc.id
        r = c.post(
            "/setup/linkedin/cookie", data={"cookie": "AQEDAR" + "x" * 120, "account_id": str(acc_id)},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200 and "Not validated yet" in r.text and "Re-check is paused" in r.text
    reset_runner()
