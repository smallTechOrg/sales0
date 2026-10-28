"""Integration D: the prospect watch runs on a LinkedIn account like discovery does (default account,
per-account cool-down and daily post cap, run row stamped, session state per account)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from sales0.clock import utcnow
from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import ScrapeRun
from sales0.jobs.scheduled_watch import start_scheduled_watch
from sales0.services import linkedin_accounts, tracked_profiles
from tests.integration.services.helpers import set_session_status
from tests.integration.watch.helpers import add_profile


@pytest.fixture(autouse=True)
def _env(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "60")
    reset_settings()
    set_session_status("connected")
    add_profile("in/asha-rao")


def _second_account() -> int:
    with session_scope() as db:
        acc = linkedin_accounts.add_account(db, "Second")
        linkedin_accounts.mark_connected(db, acc.id)
        return acc.id


def _abort(account_id: int, kind: str = "captcha") -> None:
    with session_scope() as db:
        db.add(ScrapeRun(trigger="manual", mode="live", status="aborted", abort_reason=kind, started_at=utcnow(),
                         finished_at=utcnow(), linkedin_account_id=account_id))


def test_eligibility_is_per_account_cooldown() -> None:
    second = _second_account()
    with session_scope() as db:
        default_id = linkedin_accounts.default_account_id(db)
    _abort(second)
    with session_scope() as db:
        err_second, _ = tracked_profiles.watch_eligibility(db, account_id=second)
        err_default, due = tracked_profiles.watch_eligibility(db, account_id=default_id)
    assert err_second is not None and err_second.code == "safety_cooldown"
    assert err_default is None and due == 1  # the other account's cool-down does not block the default one


def test_default_account_cooldown_blocks_a_scheduled_watch_start() -> None:
    with session_scope() as db:
        default_id = linkedin_accounts.default_account_id(db)
    _abort(default_id)
    outcome = start_scheduled_watch()
    assert not outcome.started and outcome.code == "safety_cooldown" and outcome.retry_at is not None
    assert start_scheduled_watch(_second_account()).code != "safety_cooldown"


def test_start_watch_run_stamps_the_account(monkeypatch: pytest.MonkeyPatch) -> None:
    from sales0.jobs import watch_job

    seen: list[int] = []
    monkeypatch.setattr(watch_job, "execute", lambda run_id, **_kw: seen.append(run_id))
    with session_scope() as db:
        default_id = linkedin_accounts.default_account_id(db)
        view = tracked_profiles.start_watch_run(db)
        rid = view.id
    with session_scope() as db:
        assert db.scalar(select(ScrapeRun.linkedin_account_id).where(ScrapeRun.id == rid)) == default_id
    from sales0.jobs.runner import get_runner

    get_runner().wait(10)
