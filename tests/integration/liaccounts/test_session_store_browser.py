"""SessionStore backends on a real headless browser against the FIXTURE LinkedIn (never linkedin.com).

* DB backend round trip: storage_state saved ENCRYPTED -> loaded into a FRESH context -> session valid.
* Folder accounts keep independent profile directories.
* Paste-cookie validation (success and failure) through the real Re-check job.
* The discovery searcher opens its context from the account's session store.
"""

from __future__ import annotations

import json

import pytest

from sales0.config import Settings
from sales0.db.engine import session_scope
from sales0.jobs import check_job
from sales0.scraper.browser import first_page, profile_dir
from sales0.scraper.search import LinkedInSearcher
from sales0.scraper.session_check import check_session
from sales0.scraper.session_store import DbSessionStore, FolderSessionStore, open_account_context, store_for
from sales0.scraper.urls import base_url
from sales0.services import linkedin_accounts

pytestmark = pytest.mark.browser

PASTED = "AQEDARFIXTURECOOKIEVALUE" + "a" * 40


def _db_account(label: str = "Server") -> int:
    with session_scope() as db:
        return linkedin_accounts.add_account(db, label, backend="db").id


def test_db_backend_round_trip_saves_state_encrypted_and_a_fresh_context_is_logged_in(
    fixture_settings: Settings, db_url: str
) -> None:
    base = base_url(fixture_settings)
    account_id = _db_account()
    # 1) a brand-new db account has no session
    handle = linkedin_accounts.build_handle(account_id)
    assert isinstance(store_for(handle), DbSessionStore)
    assert check_session(fixture_settings, handle).reason == "no_cookie"

    # 2) log in through the fixture; closing the context saves the refreshed state
    handle = linkedin_accounts.build_handle(account_id)
    with open_account_context(fixture_settings, handle, headless=True) as context:
        page = first_page(context)
        page.goto(f"{base}/login?direct=1", wait_until="domcontentloaded")
        assert page.url == f"{base}/feed/"
        cookie_value = next(c["value"] for c in context.cookies(base) if c["name"] == "li_at")
    with session_scope() as db:
        row = linkedin_accounts.get_account(db, account_id)
        ciphertext = row.session_state
        assert ciphertext and cookie_value not in ciphertext and "li_at" not in ciphertext
        stored = json.loads(linkedin_accounts.decrypt_state(row) or "{}")
        assert any(c["name"] == "li_at" and c["value"] == cookie_value for c in stored["cookies"])

    # 3) a FRESH context built from the stored state is a valid session (no profile directory involved)
    fresh = linkedin_accounts.build_handle(account_id)
    assert fresh.spec.storage_state and cookie_value in fresh.spec.storage_state
    result = check_session(fixture_settings, fresh)
    assert (result.logged_in, result.reason) == (True, "ok")
    assert not any(p.name.startswith("linkedin-account") for p in fixture_settings.data_path.glob("*"))


def test_db_backend_does_not_touch_the_default_folder_profile(fixture_settings: Settings, db_url: str) -> None:
    account_id = _db_account()
    check_session(fixture_settings, linkedin_accounts.build_handle(account_id))
    assert not profile_dir(fixture_settings, "chromium").exists()


def test_a_cookie_set_in_one_db_account_is_invisible_to_another(fixture_settings: Settings, db_url: str) -> None:
    base = base_url(fixture_settings)
    one, two = _db_account("One"), _db_account("Two")
    with open_account_context(fixture_settings, linkedin_accounts.build_handle(one), headless=True) as context:
        first_page(context).goto(f"{base}/login?direct=1", wait_until="domcontentloaded")
    assert check_session(fixture_settings, linkedin_accounts.build_handle(one)).logged_in is True
    assert check_session(fixture_settings, linkedin_accounts.build_handle(two)).reason == "no_cookie"


def test_folder_accounts_use_separate_profile_directories(fixture_settings: Settings, db_url: str) -> None:
    base = base_url(fixture_settings)
    with session_scope() as db:
        default = linkedin_accounts.default_account_id(db)
        other = linkedin_accounts.add_account(db, "Other", backend="folder").id
    h_default, h_other = linkedin_accounts.build_handle(default), linkedin_accounts.build_handle(other)
    assert isinstance(store_for(h_default), FolderSessionStore)
    # the default account keeps the pre-p8 directory; the other has its own, per mode and channel
    assert profile_dir(fixture_settings, "chromium", h_default.spec) == profile_dir(fixture_settings, "chromium")
    other_dir = profile_dir(fixture_settings, "chromium", h_other.spec)
    assert other_dir != profile_dir(fixture_settings, "chromium")
    assert other_dir.name == f"linkedin-account-{other}-fixture-chromium"

    with open_account_context(fixture_settings, h_other, headless=True) as context:
        first_page(context).goto(f"{base}/login?direct=1", wait_until="domcontentloaded")
    assert check_session(fixture_settings, h_other).logged_in is True
    assert check_session(fixture_settings, h_default).reason == "no_cookie"
    assert other_dir.is_dir()


def test_pinned_identity_is_applied_to_the_context(fixture_settings: Settings, db_url: str) -> None:
    account_id = _db_account()
    with session_scope() as db:
        linkedin_accounts.set_pins(db, account_id, user_agent="PinnedUA/9.9", locale="de-DE",
                                   timezone="Europe/Berlin", viewport="1111x777")
    with open_account_context(fixture_settings, linkedin_accounts.build_handle(account_id), headless=True) as context:
        page = first_page(context)
        page.goto(f"{base_url(fixture_settings)}/authwall", wait_until="domcontentloaded")
        info = page.evaluate(
            "() => ({ua: navigator.userAgent, lang: navigator.language, "
            "tz: Intl.DateTimeFormat().resolvedOptions().timeZone, w: innerWidth, h: innerHeight})"
        )
    assert info == {"ua": "PinnedUA/9.9", "lang": "de-DE", "tz": "Europe/Berlin", "w": 1111, "h": 777}


# ---------------------------------------------------------------- paste cookie -> validate
def test_pasted_cookie_validates_through_one_feed_load_and_connects_the_account(
    fixture_settings: Settings, db_url: str
) -> None:
    with session_scope() as db:
        view = linkedin_accounts.paste_cookie(db, f"li_at={PASTED}", label="Server")
    check_job.execute(linkedin_account_id=view.id)
    with session_scope() as db:
        after = linkedin_accounts.account_view(db, view.id)
        row = linkedin_accounts.get_account(db, view.id)
        assert (after.status, after.last_error) == ("connected", None)
        assert after.last_ok_at is not None
        assert PASTED not in (row.session_state or "")  # still ciphertext after the state was saved back
        assert PASTED in (linkedin_accounts.decrypt_state(row) or "")


def test_a_rejected_pasted_cookie_leaves_the_account_not_connected_and_forgets_it(
    fixture_settings: Settings, db_url: str
) -> None:
    with session_scope() as db:
        view = linkedin_accounts.paste_cookie(db, "invalid" + "x" * 30, label="Bad")
    check_job.execute(linkedin_account_id=view.id)
    with session_scope() as db:
        after = linkedin_accounts.account_view(db, view.id)
        assert after.status == "not_connected"
        assert after.last_error and "Not logged in" in after.last_error
        assert after.has_session_state is False  # the unaccepted cookie is not kept


def test_an_expired_cookie_on_a_connected_account_needs_attention(fixture_settings: Settings, db_url: str) -> None:
    with session_scope() as db:
        view = linkedin_accounts.paste_cookie(db, PASTED, label="Server")
    check_job.execute(linkedin_account_id=view.id)  # connected
    with session_scope() as db:
        linkedin_accounts.paste_cookie(db, "expired" + "y" * 30, account_id=view.id)
        linkedin_accounts.get_account(db, view.id).status = "connected"  # it was connected before LinkedIn expired it
    check_job.execute(linkedin_account_id=view.id)
    with session_scope() as db:
        after = linkedin_accounts.account_view(db, view.id)
        assert after.status == "needs_attention" and after.safety_state == "logout"


# ---------------------------------------------------------------- the searcher uses the account's store
def test_the_searcher_opens_its_context_from_the_accounts_session_store(fixture_settings: Settings, db_url: str) -> None:
    with session_scope() as db:
        view = linkedin_accounts.paste_cookie(db, PASTED, label="Server")
    handle = linkedin_accounts.build_handle(view.id)
    with LinkedInSearcher(fixture_settings, account=handle) as searcher:
        assert searcher.session is not None and searcher.session.logged_in
    # a different db account without a cookie has no session
    other = _db_account("Empty")
    from sales0.scraper.errors import SessionMissing

    with pytest.raises(SessionMissing), LinkedInSearcher(fixture_settings, account=linkedin_accounts.build_handle(other)):
        pass
