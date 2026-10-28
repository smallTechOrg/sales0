"""Setup page: the LinkedIn accounts card (list / add / rename / disable / paste cookie), over HTTP on a
migrated SQLite file. Browser jobs are replaced by recorders (no browser, no LinkedIn)."""

from __future__ import annotations

from html import unescape

import pytest
from fastapi.testclient import TestClient

from sales0.db.engine import session_scope
from sales0.services import linkedin_accounts
from tests.integration.liaccounts.helpers import new_account
from tests.integration.web.conftest import HX


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Record check / connect job starts instead of opening a browser."""
    from sales0.jobs import check_job, connect_job

    started: list[tuple[str, dict]] = []
    monkeypatch.setattr(check_job, "execute", lambda *a, **k: started.append(("check", k)))
    monkeypatch.setattr(connect_job, "execute", lambda *a, **k: started.append(("connect", k)))
    return started


def _card(client: TestClient) -> str:
    html = unescape(client.get("/setup").text)
    return html.split('id="linkedin-accounts"', 1)[1].split("</section>", 1)[0]


def _wait_idle() -> None:
    from sales0.jobs.runner import get_runner

    assert get_runner().wait(10)


def test_single_account_setup_keeps_the_old_card_and_lists_one_default_account(client: TestClient) -> None:
    html = unescape(client.get("/setup").text)
    old = html.split('id="linkedin-card"', 1)[1].split("</section>", 1)[0]
    assert 'id="connect-linkedin"' in old and 'id="check-linkedin"' in old
    card = _card(client)
    assert "1 account" in card and "Default" in card and "default" in card
    # the default account is driven by the main card: no duplicate Connect / Re-check buttons here
    assert "Connect Default" not in card and "Re-check Default" not in card
    assert 'id="paste-cookie"' in card and "Paste li_at cookie" in card
    for warning in ("act as you", "Terms of Service", "datacentre", "SALES0_SECRET_KEY", "stored <strong>encrypted"):
        assert warning in card, warning


def test_add_rename_disable_and_enable_an_account(client: TestClient, jobs) -> None:  # noqa: ANN001
    added = client.post("/setup/linkedin/accounts", data={"label": "Priya"}, headers=HX)
    assert added.status_code == 200 and "Added &#39;Priya&#39;" in added.text or "Added 'Priya'" in unescape(added.text)
    with session_scope() as db:
        accounts = linkedin_accounts.list_accounts(db)
    assert [a.label for a in accounts] == ["Default", "Priya"]
    priya = accounts[1].id
    card = _card(client)
    assert "Connect Priya" in card and "Re-check Priya" in card  # non-default rows carry their own buttons

    renamed = client.post(f"/setup/linkedin/accounts/{priya}/rename", data={"label": "Priya M"}, headers=HX)
    assert "Priya M" in unescape(renamed.text)
    disabled = client.post(f"/setup/linkedin/accounts/{priya}/disable", headers=HX)
    assert 'data-status="disabled"' in disabled.text and "Disabled" in disabled.text
    assert "Connect Priya M" not in unescape(disabled.text)
    enabled = client.post(f"/setup/linkedin/accounts/{priya}/enable", headers=HX)
    assert 'data-status="not_connected"' in enabled.text


def test_the_last_enabled_account_cannot_be_disabled(client: TestClient) -> None:
    with session_scope() as db:
        only = linkedin_accounts.default_account_id(db)
    resp = client.post(f"/setup/linkedin/accounts/{only}/disable", headers=HX)
    assert resp.headers.get("HX-Retarget") == "#toasts" and "At least one LinkedIn account must stay enabled" in unescape(resp.text)


def test_blank_label_is_refused(client: TestClient) -> None:
    resp = client.post("/setup/linkedin/accounts", data={"label": "   "}, headers=HX)
    assert resp.headers.get("HX-Retarget") == "#toasts" and "Give the LinkedIn account a name" in unescape(resp.text)


def test_connect_and_recheck_target_the_chosen_account(client: TestClient, jobs) -> None:  # noqa: ANN001
    other = new_account("Other", status="not_connected")
    assert client.post(f"/setup/linkedin/accounts/{other}/connect", headers=HX).status_code == 200
    _wait_idle()
    assert client.post(f"/setup/linkedin/accounts/{other}/check", headers=HX).status_code == 200
    _wait_idle()
    # the main card still drives the default account
    client.post("/setup/linkedin/check", headers=HX)
    _wait_idle()
    assert jobs == [
        ("connect", {"linkedin_account_id": other}),
        ("check", {"linkedin_account_id": other}),
        ("check", {"linkedin_account_id": jobs[2][1]["linkedin_account_id"]}),
    ]
    with session_scope() as db:
        assert jobs[2][1]["linkedin_account_id"] == linkedin_accounts.default_account_id(db) != other


def test_a_disabled_account_refuses_connect(client: TestClient, jobs) -> None:  # noqa: ANN001
    other = new_account("Other", status="not_connected")
    with session_scope() as db:
        linkedin_accounts.disable_account(db, other)
    resp = client.post(f"/setup/linkedin/accounts/{other}/connect", headers=HX)
    assert resp.headers.get("HX-Retarget") == "#toasts" and "disabled" in unescape(resp.text)
    assert jobs == []


def test_paste_cookie_creates_a_db_account_stores_it_encrypted_and_starts_a_validation(client: TestClient, jobs) -> None:  # noqa: ANN001
    secret = "AQEDARPASTEDCOOKIE" + "k" * 30
    resp = client.post("/setup/linkedin/cookie", data={"cookie": f"li_at={secret}", "label": "Server"}, headers=HX)
    assert resp.status_code == 200 and "validating" in unescape(resp.text)
    assert secret not in resp.text  # never echoed back
    _wait_idle()
    with session_scope() as db:
        accounts = linkedin_accounts.list_accounts(db)
        server = next(a for a in accounts if a.label == "Server")
        row = linkedin_accounts.get_account(db, server.id)
        assert server.backend == "db" and server.has_session_state
        assert secret not in (row.session_state or "")
    assert jobs == [("check", {"linkedin_account_id": server.id})]
    assert "pasted cookie" in _card(client)


def test_a_malformed_cookie_is_refused_and_nothing_is_stored(client: TestClient, jobs) -> None:  # noqa: ANN001
    resp = client.post("/setup/linkedin/cookie", data={"cookie": "not a cookie", "label": "Bad"}, headers=HX)
    assert resp.headers.get("HX-Retarget") == "#toasts" and "does not look like a li_at cookie" in unescape(resp.text)
    with session_scope() as db:
        assert [a.label for a in linkedin_accounts.list_accounts(db)] == ["Default"]
    assert jobs == []


def test_pasting_onto_a_folder_account_is_refused(client: TestClient, jobs) -> None:  # noqa: ANN001
    with session_scope() as db:
        folder = linkedin_accounts.default_account_id(db)
    resp = client.post("/setup/linkedin/cookie", data={"cookie": "AQEDAR" + "q" * 30, "account_id": str(folder)}, headers=HX)
    assert resp.headers.get("HX-Retarget") == "#toasts" and "Chrome window" in unescape(resp.text)


def test_non_htmx_posts_redirect_back_to_setup(client: TestClient, jobs) -> None:  # noqa: ANN001
    resp = client.post("/setup/linkedin/accounts", data={"label": "Plain"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/setup"
