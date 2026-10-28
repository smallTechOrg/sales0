"""LinkedIn accounts: service layer, per-account cool-down / daily caps / counters, cookie handling.

Real SQLite file migrated to head (the p8 migration seeds the default account); no browser.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import LinkedInAccount, ScrapeRun
from sales0.errors import DomainError
from sales0.jobs import discovery_job
from sales0.services import keywords, linkedin_accounts, runs
from sales0.services import session as session_service
from tests.integration.liaccounts.helpers import T0, add_run, new_account, set_default_status
from tests.integration.services.helpers import FakeSearcher, post, safety
from tests.support.fake_llm import FakeLLMClient, triage_answer


@pytest.fixture(autouse=True)
def _cooldowns(_isolated_runtime: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "60")
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN", "15")
    reset_settings()


def _keyword() -> None:
    with session_scope() as db:
        keywords.add_keyword(db, "ai workshop")


def _eligibility(account_id: int | None, at=T0 + timedelta(minutes=5)):  # type: ignore[no-untyped-def]
    with session_scope() as db:
        return runs.run_eligibility(db, now=at, account_id=account_id)


# ---------------------------------------------------------------- the account list
def test_migrated_database_has_one_default_account(db_url: str) -> None:
    with session_scope() as db:
        accounts = linkedin_accounts.list_accounts(db)
        assert [(a.label, a.is_default, a.backend, a.status) for a in accounts] == [("Default", True, "folder", "not_connected")]
        assert linkedin_accounts.default_account_id(db) == accounts[0].id


def test_add_rename_disable_enable_and_the_default_follows_the_first_active(db_url: str) -> None:
    with session_scope() as db:
        first = linkedin_accounts.default_account_id(db)
        second = linkedin_accounts.add_account(db, "  Priya   work ", backend="db")
        assert second.label == "Priya work" and second.backend == "db" and not second.is_default
        assert linkedin_accounts.rename_account(db, second.id, "Priya").label == "Priya"
        folder = linkedin_accounts.add_account(db, "Chrome one")
        assert folder.backend == "folder"  # SALES0_LINKEDIN_SESSION_BACKEND defaults to folder
        assert linkedin_accounts.get_account(db, folder.id).profile_dirname == f"linkedin-account-{folder.id}"

        assert linkedin_accounts.disable_account(db, first).status == "disabled"
        assert linkedin_accounts.default_account_id(db) == second.id  # first ACTIVE account
        assert linkedin_accounts.enable_account(db, first).status == "not_connected"
        assert linkedin_accounts.default_account_id(db) == first


def test_the_last_active_account_cannot_be_disabled_and_labels_are_validated(db_url: str) -> None:
    with session_scope() as db:
        only = linkedin_accounts.default_account_id(db)
        with pytest.raises(DomainError) as last:
            linkedin_accounts.disable_account(db, only)
        assert last.value.code == "last_account"
        with pytest.raises(DomainError) as blank:
            linkedin_accounts.add_account(db, "   ")
        assert blank.value.code == "account_label_required"
        with pytest.raises(DomainError) as long:
            linkedin_accounts.rename_account(db, only, "x" * 81)
        assert long.value.code == "account_label_invalid"
        with pytest.raises(DomainError) as missing:
            linkedin_accounts.rename_account(db, 9999, "Ghost")
        assert missing.value.code == "not_found"


def test_pins_are_validated_and_a_proxy_is_never_exposed(db_url: str) -> None:
    with session_scope() as db:
        aid = linkedin_accounts.default_account_id(db)
        view = linkedin_accounts.set_pins(
            db, aid, proxy_url="http://user:secret@proxy.example:8080", user_agent="UA/1", locale="en-IN",
            timezone="Asia/Kolkata", viewport="1280X800",
        )
        assert (view.proxy_set, view.user_agent, view.locale, view.timezone, view.viewport) == (True, "UA/1", "en-IN", "Asia/Kolkata", "1280x800")
        assert "secret" not in view.model_dump_json()
        spec = linkedin_accounts.spec_for(linkedin_accounts.get_account(db, aid))
        assert spec.viewport == (1280, 800) and spec.proxy_url == "http://user:secret@proxy.example:8080"
        with pytest.raises(DomainError):
            linkedin_accounts.set_pins(db, aid, proxy_url="not a url")
        with pytest.raises(DomainError):
            linkedin_accounts.set_pins(db, aid, viewport="big")


# ---------------------------------------------------------------- per-account cool-down
def test_one_account_in_cooldown_does_not_block_the_other(db_url: str) -> None:
    _keyword()
    first = set_default_status("connected")
    second = new_account("Second")
    add_run(second, status="aborted", abort="captcha")  # the second account hit a captcha at T0

    blocked = _eligibility(second)
    assert (blocked.can_run, blocked.reason_code) == (False, "safety_cooldown")
    free = _eligibility(first)
    assert (free.can_run, free.reason_code) == (True, None)
    with session_scope() as db:
        assert runs.safety_cooldown_until(db, T0 + timedelta(minutes=5), first) is None
        assert runs.safety_cooldown_until(db, T0 + timedelta(minutes=5), second) == T0 + timedelta(minutes=60)
        # the default account (account_id=None) is the first one
        assert runs.safety_cooldown_until(db, T0 + timedelta(minutes=5)) is None
    # the cool-down ends for the second account on its own clock
    assert _eligibility(second, T0 + timedelta(minutes=61)).can_run is True


def test_a_cooldown_on_the_default_account_includes_unstamped_runs_and_spares_the_other(db_url: str) -> None:
    _keyword()
    first = set_default_status("connected")
    second = new_account("Second")
    add_run(None, status="aborted", abort="rate_limited")  # written by pre-p8 code / a bare test row
    assert _eligibility(first).reason_code == "safety_cooldown"
    assert _eligibility(None).reason_code == "safety_cooldown"
    assert _eligibility(second).can_run is True


def test_start_run_is_refused_for_the_cooled_account_only_and_stamps_the_account(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _keyword()
    first = set_default_status("connected")
    second = new_account("Second")
    add_run(second, status="aborted", abort="checkpoint", at=__import__("sales0.clock", fromlist=["utcnow"]).utcnow())
    submitted: list[str] = []
    from sales0.jobs.runner import get_runner

    monkeypatch.setattr(get_runner(), "submit", lambda name, fn: submitted.append(name))
    with session_scope() as db:
        with pytest.raises(DomainError) as stopped:
            runs.start_run(db, linkedin_account_id=second)
        assert stopped.value.code == "safety_cooldown"
        view = runs.start_run(db, linkedin_account_id=first)
        assert db.get(ScrapeRun, view.id).linkedin_account_id == first
    assert submitted == ["run"]


def test_a_disabled_account_cannot_run(db_url: str) -> None:
    _keyword()
    set_default_status("connected")
    second = new_account("Second")
    with session_scope() as db:
        linkedin_accounts.disable_account(db, second)
    assert _eligibility(second).reason_code == "account_disabled"


# ---------------------------------------------------------------- per-account safety state through the real job
def test_a_safety_stop_marks_only_the_account_that_ran(db_url: str) -> None:
    _keyword()
    first = set_default_status("connected")
    second = new_account("Second")
    with session_scope() as db:
        run = ScrapeRun(trigger="manual", mode="fixture", status="queued", started_at=T0, linkedin_account_id=second)
        db.add(run)
        db.flush()
        rid = run.id
    searcher = FakeSearcher({"ai workshop": safety("captcha")})
    discovery_job.execute(rid, llm=FakeLLMClient(responder=lambda req: triage_answer()), searcher_factory=searcher, sleep=lambda _s: None)
    with session_scope() as db:
        one, two = linkedin_accounts.account_view(db, first), linkedin_accounts.account_view(db, second)
        assert (one.status, one.safety_state) == ("connected", None)
        assert (two.status, two.safety_state) == ("needs_attention", "captcha")
        assert two.cooldown_until is not None  # snapshot mirrored onto the row
        assert db.get(ScrapeRun, rid).abort_reason == "captcha"
    assert _eligibility(first, utc_now_plus()).can_run is True


def utc_now_plus():  # type: ignore[no-untyped-def]
    from sales0.clock import utcnow

    return utcnow() + timedelta(minutes=1)


def test_an_unstamped_queued_run_is_stamped_with_the_default_account(db_url: str) -> None:
    _keyword()
    first = set_default_status("connected")
    with session_scope() as db:
        run = ScrapeRun(trigger="manual", mode="fixture", status="queued", started_at=T0)
        db.add(run)
        db.flush()
        rid = run.id
    searcher = FakeSearcher({"ai workshop": [post(1, "need ai workshop")]})
    discovery_job.execute(rid, llm=FakeLLMClient(responder=lambda req: triage_answer()), searcher_factory=searcher, sleep=lambda _s: None)
    with session_scope() as db:
        run = db.get(ScrapeRun, rid)
        assert run.linkedin_account_id == first and run.status == "succeeded"
        assert linkedin_accounts.get_account(db, first).last_successful_run_at is not None


# ---------------------------------------------------------------- per-account daily counters
def test_daily_run_and_post_counters_are_per_account(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_MAX_RUNS_PER_DAY", "2")
    monkeypatch.setenv("SALES0_SCRAPE_MAX_POSTS_PER_DAY", "50")
    reset_settings()
    _keyword()
    first = set_default_status("connected")
    second = new_account("Second")
    add_run(first, posts_seen=30)
    add_run(first, posts_seen=25)
    add_run(second, posts_seen=3)

    with session_scope() as db:
        assert runs._day_counts(db, T0 + timedelta(hours=1), first) == (2, 55)
        assert runs._day_counts(db, T0 + timedelta(hours=1), second) == (1, 3)
        assert runs.posts_seen_today(db, T0 + timedelta(hours=1), second) == 3
    capped = _eligibility(first, T0 + timedelta(hours=1))
    assert (capped.can_run, capped.reason_code, capped.runs_today) == (False, "daily_run_cap", 2)
    ok = _eligibility(second, T0 + timedelta(hours=1))
    assert (ok.can_run, ok.runs_today, ok.posts_today) == (True, 1, 3)

    with session_scope() as db:
        runs.sync_account_state(db, first, T0 + timedelta(hours=1))
        runs.sync_account_state(db, second, T0 + timedelta(hours=1))
        a, b = linkedin_accounts.account_view(db, first), linkedin_accounts.account_view(db, second)
        assert (a.runs_today, a.posts_today, b.runs_today, b.posts_today) == (2, 55, 1, 3)


def test_the_daily_post_cap_is_per_account(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_MAX_POSTS_PER_DAY", "20")
    reset_settings()
    _keyword()
    first = set_default_status("connected")
    second = new_account("Second")
    add_run(first, posts_seen=20)
    assert _eligibility(first, T0 + timedelta(hours=1)).reason_code == "daily_post_cap"
    assert _eligibility(second, T0 + timedelta(hours=1)).can_run is True


# ---------------------------------------------------------------- session facade keeps working per account
def test_session_status_and_transitions_are_per_account(db_url: str) -> None:
    first = set_default_status("connected")
    second = new_account("Second", status="not_connected")
    with session_scope() as db:
        session_service.mark_needs_attention(db, "checkpoint", "https://x/checkpoint/", account_id=second)
        assert session_service.session_status(db).stored_status == "connected"
        assert session_service.session_status(db, second).stored_status == "needs_attention"
        session_service.mark_connected(db, connected=True, account_id=second)
        assert session_service.session_status(db, second).last_connected_at is not None
        assert session_service.get_session_row(db).id == first


# ---------------------------------------------------------------- cookie handling
def test_clean_cookie_accepts_pasted_variants_and_rejects_junk() -> None:
    value = "AQEDAR" + "x" * 40
    for pasted in (value, f"li_at={value}", f' "{value}" ', f"li_at={value};"):
        assert linkedin_accounts.clean_cookie(pasted) == value
    for bad in ("", "short", "has space inside the cookie value", "a;b;c;d;e;f;g;h", "x" * 2000):
        with pytest.raises(DomainError) as err:
            linkedin_accounts.clean_cookie(bad)
        assert err.value.code == "cookie_invalid"


def test_paste_cookie_stores_it_encrypted_on_a_db_account(db_url: str) -> None:
    secret = "AQEDARSECRETCOOKIEVALUE" + "z" * 30
    with session_scope() as db:
        view = linkedin_accounts.paste_cookie(db, f"li_at={secret}", label="Server")
        row = linkedin_accounts.get_account(db, view.id)
        assert (view.backend, view.status, view.has_session_state) == ("db", "not_connected", True)
        assert secret not in (row.session_state or "")
        assert secret in (linkedin_accounts.decrypt_state(row) or "")
        # pasting again onto the same account replaces the cookie
        again = linkedin_accounts.paste_cookie(db, "AQEDARNEWCOOKIE" + "n" * 20, account_id=view.id)
        assert again.id == view.id
        assert "AQEDARNEWCOOKIE" in (linkedin_accounts.decrypt_state(row) or "")
        assert db.scalar(select(LinkedInAccount.id).where(LinkedInAccount.label == "Server")) == view.id


def test_a_folder_account_refuses_a_pasted_cookie(db_url: str) -> None:
    with session_scope() as db:
        folder = linkedin_accounts.default_account_id(db)
        with pytest.raises(DomainError) as err:
            linkedin_accounts.paste_cookie(db, "AQEDAR" + "q" * 30, account_id=folder)
        assert err.value.code == "account_backend_mismatch"


def test_a_public_host_needs_a_secret_key_to_store_a_cookie(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_HOST", "0.0.0.0")
    monkeypatch.delenv("SALES0_SECRET_KEY", raising=False)
    monkeypatch.setenv("SALES0_APP_PASSWORD", "pw")
    reset_settings()
    with session_scope() as db:
        with pytest.raises(DomainError) as err:
            linkedin_accounts.paste_cookie(db, "AQEDAR" + "q" * 30)
        assert err.value.code == "secret_key_required"
    monkeypatch.setenv("SALES0_SECRET_KEY", "a-real-server-secret")
    reset_settings()
    with session_scope() as db:
        assert linkedin_accounts.paste_cookie(db, "AQEDAR" + "q" * 30).backend == "db"


def test_a_cookie_that_cannot_be_decrypted_is_treated_as_absent(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SECRET_KEY", "key-one-0123456789abcdef")
    reset_settings()
    with session_scope() as db:
        view = linkedin_accounts.paste_cookie(db, "AQEDAR" + "q" * 30)
    monkeypatch.setenv("SALES0_SECRET_KEY", "key-two-0123456789abcdef")
    reset_settings()
    with session_scope() as db:
        assert linkedin_accounts.decrypt_state(linkedin_accounts.get_account(db, view.id)) is None
        assert linkedin_accounts.spec_for(linkedin_accounts.get_account(db, view.id)).storage_state is None
