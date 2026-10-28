"""Per-user accounts through the real app: Users page, set-password links, roles, sessions, acting-as, open mode."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sales0.db.engine import session_scope
from sales0.db.models import MemberSession, PasswordToken, StatusHistory, TeamMember, TimelineEvent
from sales0.web import auth
from sales0.web.app import create_app
from tests.integration.members.conftest import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    LINK,
    MEMBER_PASSWORD,
    activate,
    add_member,
    login,
)
from tests.integration.web.conftest import make_enquiry, triaged

HX = {"HX-Request": "true"}


def _member_id(email: str) -> int:
    with session_scope() as db:
        return db.scalar(select(TeamMember.id).where(TeamMember.email == email))


# ---------------------------------------------------------------------- bootstrap + login
def test_bootstrap_password_creates_first_admin_and_logs_in(admin: TestClient) -> None:
    with session_scope() as db:
        a = db.scalar(select(TeamMember).where(TeamMember.email == ADMIN_EMAIL))
        assert a.role == "admin" and a.name == "Boss Admin" and a.password_hash.startswith("$argon2id$")
        assert a.last_login_at is not None and ADMIN_PASSWORD not in a.password_hash
    page = admin.get("/inbox")
    assert 'id="whoami"' in page.text and "Boss Admin" in page.text and 'id="logout"' in page.text
    assert 'id="open-mode-banner"' not in page.text


def test_login_needs_email_and_unknown_or_inactive_fail_the_same(make_client: Callable[[], TestClient]) -> None:
    c = make_client()
    for email, pw in ((ADMIN_EMAIL, "wrong-wrong-1"), ("nobody@acct.example", ADMIN_PASSWORD), ("", ADMIN_PASSWORD)):
        r = login(c, email, pw)
        assert r.status_code == 401 and "Wrong email or password." in r.text and "set-cookie" not in r.headers


def test_session_cookie_flags_and_remember_me(make_client: Callable[[], TestClient]) -> None:
    c = make_client()
    r = login(c, ADMIN_EMAIL, ADMIN_PASSWORD)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and f"max-age={14 * 86400}" in cookie
    c2 = make_client()
    r = login(c2, ADMIN_EMAIL, ADMIN_PASSWORD, remember="1")
    assert f"max-age={30 * 86400}" in r.headers["set-cookie"].lower()
    with session_scope() as db:
        assert sorted(s.remember for s in db.scalars(select(MemberSession))) == [False, True]
        assert all(len(s.sid_hash) == 64 for s in db.scalars(select(MemberSession)))  # only hashes are stored


def test_session_rotates_and_old_cookie_expires_after_grace(admin: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    old = admin.cookies[auth.COOKIE]
    real = auth.now
    monkeypatch.setattr(auth, "now", lambda: real() + timedelta(minutes=20))
    r = admin.get("/inbox")
    assert r.status_code == 200 and f"{auth.COOKIE}=" in r.headers["set-cookie"]
    new = admin.cookies[auth.COOKIE]
    assert new != old
    # the previous id still works within the grace window, then not
    other = TestClient(admin.app, follow_redirects=False)
    other.cookies.set(auth.COOKIE, old)
    assert other.get("/inbox").status_code == 200
    monkeypatch.setattr(auth, "now", lambda: real() + timedelta(minutes=25))
    other2 = TestClient(admin.app, follow_redirects=False)
    other2.cookies.set(auth.COOKIE, old)
    assert other2.get("/inbox").status_code == 303


def test_remember_session_lives_past_normal_expiry(make_client: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch) -> None:
    short, long_ = make_client(), make_client()
    login(short, ADMIN_EMAIL, ADMIN_PASSWORD)
    login(long_, ADMIN_EMAIL, ADMIN_PASSWORD, remember="1")
    real = auth.now
    monkeypatch.setattr(auth, "now", lambda: real() + timedelta(days=20))
    assert short.get("/inbox").status_code == 303
    assert long_.get("/inbox").status_code == 200


def test_logout_revokes_the_server_side_session(admin: TestClient) -> None:
    stolen = admin.cookies[auth.COOKIE]
    assert admin.post("/logout").status_code == 303
    thief = TestClient(admin.app, follow_redirects=False)
    thief.cookies.set(auth.COOKIE, stolen)
    assert thief.get("/inbox").status_code == 303
    with session_scope() as db:
        assert db.scalar(select(func.count(MemberSession.id))) == 0


# ---------------------------------------------------------------------- users page + links
def test_users_page_admin_only_and_nav(admin: TestClient, make_client: Callable[[], TestClient]) -> None:
    assert 'href="/users"' in admin.get("/inbox").text
    link = add_member(admin, "Meera Member", "meera@acct.example")
    member = activate(make_client, link, "meera@acct.example")
    assert 'href="/users"' not in member.get("/inbox").text
    assert member.get("/users").status_code == 403
    assert member.post("/users", data={"name": "X", "email": "x@acct.example"}).status_code == 403
    assert member.post("/team/members", data={"name": "X", "email": "x@acct.example"}).status_code == 403
    assert member.post(f"/users/{_member_id(ADMIN_EMAIL)}/active", data={"active": "0"}).status_code == 403


def test_link_shown_once_stored_hashed_single_use(admin: TestClient, make_client: Callable[[], TestClient]) -> None:
    link = add_member(admin, "Meera Member", "meera@acct.example")
    token = link.rsplit("/", 1)[1]
    assert token not in admin.get("/users").text  # never shown again
    with session_scope() as db:
        row = db.scalar(select(PasswordToken))
        assert row.token_hash != token and token not in repr(row.__dict__) and row.used_at is None
    c = make_client()
    page = c.get(link)
    assert page.status_code == 200 and 'id="set-password-form"' in page.text and "Meera Member" in page.text
    # weak and mismatched passwords are refused and do not burn the link
    assert c.post(link, data={"password": "short", "confirm": "short"}).status_code == 400
    assert "do not match" in c.post(link, data={"password": MEMBER_PASSWORD, "confirm": MEMBER_PASSWORD + "x"}).text
    done = c.post(link, data={"password": MEMBER_PASSWORD, "confirm": MEMBER_PASSWORD})
    assert done.status_code == 303 and done.headers["location"] == "/login"
    assert "Password set" in c.get("/login").text  # the flash survives the redirect
    again = c.get(link)
    assert again.status_code == 410 and "expired or was already used" in again.text
    assert c.post(link, data={"password": "another-long-password", "confirm": "another-long-password"}).status_code == 410
    assert login(c, "meera@acct.example", MEMBER_PASSWORD).status_code == 303


def test_link_expires(admin: TestClient, make_client: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch) -> None:
    link = add_member(admin, "Meera Member", "meera@acct.example")
    real = auth.now
    monkeypatch.setattr(auth, "now", lambda: real() + timedelta(hours=25))
    assert make_client().get(link).status_code == 410


def test_new_link_invalidates_the_previous_one(admin: TestClient, make_client: Callable[[], TestClient]) -> None:
    first = add_member(admin, "Meera Member", "meera@acct.example")
    mid = _member_id("meera@acct.example")
    r = admin.post(f"/users/{mid}/reset")
    second = "/set-password/" + LINK.search(r.text).group(1).rsplit("/set-password/", 1)[1]
    assert first != second
    c = make_client()
    assert c.get(first).status_code == 410
    assert c.get(second).status_code == 200


def test_force_reset_blocks_old_password_and_kills_sessions(admin: TestClient, make_client: Callable[[], TestClient]) -> None:
    member = activate(make_client, add_member(admin, "Meera Member", "meera@acct.example"), "meera@acct.example")
    assert member.get("/inbox").status_code == 200
    r = admin.post(f"/users/{_member_id('meera@acct.example')}/reset")
    link = "/set-password/" + LINK.search(r.text).group(1).rsplit("/set-password/", 1)[1]
    assert member.get("/inbox").status_code == 303  # session revoked
    assert login(member, "meera@acct.example", MEMBER_PASSWORD).status_code == 401  # old password blocked until reset
    fresh = make_client()
    assert fresh.post(link, data={"password": "brand-new-pass-9", "confirm": "brand-new-pass-9"}).status_code == 303
    assert login(fresh, "meera@acct.example", "brand-new-pass-9").status_code == 303


def test_deactivate_kills_session_and_blocks_login(admin: TestClient, make_client: Callable[[], TestClient]) -> None:
    member = activate(make_client, add_member(admin, "Meera Member", "meera@acct.example"), "meera@acct.example")
    mid = _member_id("meera@acct.example")
    assert member.get("/inbox").status_code == 200
    assert admin.post(f"/users/{mid}/active", data={"active": "0"}).status_code == 303
    assert member.get("/inbox").status_code == 303
    assert login(member, "meera@acct.example", MEMBER_PASSWORD).status_code == 401
    with session_scope() as db:
        assert db.scalar(select(func.count(MemberSession.id)).where(MemberSession.member_id == mid)) == 0
    admin.post(f"/users/{mid}/active", data={"active": "1"})
    assert login(member, "meera@acct.example", MEMBER_PASSWORD).status_code == 303


def test_last_admin_and_self_lockout_protection(admin: TestClient) -> None:
    me = _member_id(ADMIN_EMAIL)
    for path, data in ((f"/users/{me}/active", {"active": "0"}), (f"/users/{me}/role", {"role": "member"})):
        r = admin.post(path, data=data, headers={"Accept": "application/json"})
        assert r.status_code == 409, path
    assert admin.get("/inbox").status_code == 200
    add_member(admin, "Second Admin", "second@acct.example", "admin")
    sid = _member_id("second@acct.example")
    # still never yourself, even with another admin around
    assert admin.post(f"/users/{me}/active", data={"active": "0"}, headers={"Accept": "application/json"}).status_code == 409
    assert admin.post(f"/users/{sid}/role", data={"role": "member"}).status_code == 303  # demote the other: fine
    assert admin.post(f"/users/{sid}/role", data={"role": "admin"}).status_code == 303


def test_member_changes_own_password(admin: TestClient, make_client: Callable[[], TestClient]) -> None:
    member = activate(make_client, add_member(admin, "Meera Member", "meera@acct.example"), "meera@acct.example")
    bad = member.post("/account/password", data={"current": "nope-nope-nope", "password": "x" * 12, "confirm": "x" * 12})
    assert bad.status_code == 303
    assert "current password is not right" in member.get("/account").text
    ok = member.post("/account/password", data={"current": MEMBER_PASSWORD, "password": "second-pass-4321", "confirm": "second-pass-4321"})
    assert ok.status_code == 303 and member.get("/inbox").status_code == 200
    other = make_client()
    assert login(other, "meera@acct.example", MEMBER_PASSWORD).status_code == 401
    assert login(other, "meera@acct.example", "second-pass-4321").status_code == 303


# ---------------------------------------------------------------------- acting as
def _seed_review_enquiry() -> int:
    return make_enquiry("P1", **triaged("genuine", "Looks real"))


def test_member_decision_is_attributed_and_timeline_shows_name(admin: TestClient, make_client: Callable[[], TestClient]) -> None:
    member = activate(make_client, add_member(admin, "Meera Member", "meera@acct.example"), "meera@acct.example")
    mid = _member_id("meera@acct.example")
    eid = _seed_review_enquiry()
    r = member.post("/inbox/decide", data={"enquiry_ids": str(eid), "decision": "accept"})
    assert r.status_code == 303
    with session_scope() as db:
        hist = db.scalars(select(StatusHistory).where(StatusHistory.entity_id == eid)).all()
        assert hist and all(h.actor_user_id == mid for h in hist)
        from sales0.db.models import Enquiry

        e = db.get(Enquiry, eid)
        assert e.actor_user_id == mid and e.prospect_id
        events = db.scalars(select(TimelineEvent).where(TimelineEvent.prospect_id == e.prospect_id)).all()
        assert events and any(t.actor == "Meera Member" and t.actor_member_id == mid for t in events)
    page = member.get(f"/pipeline/prospects/{e.prospect_id}")
    assert page.status_code == 200 and "Meera Member" in page.text


def test_non_admin_cannot_act_as_someone_else(admin: TestClient, make_client: Callable[[], TestClient]) -> None:
    member = activate(make_client, add_member(admin, "Meera Member", "meera@acct.example"), "meera@acct.example")
    boss = _member_id(ADMIN_EMAIL)
    assert member.post("/team/acting", data={"member_id": str(boss), "next": "/inbox"}).status_code == 403
    # a forged / copied acting cookie is ignored for a non-admin
    from sales0.services.team import acting as acting_service

    member.cookies.set(acting_service.COOKIE_NAME, acting_service.cookie_value(boss), path="/")
    eid = _seed_review_enquiry()
    member.post("/inbox/decide", data={"enquiry_ids": str(eid), "decision": "accept"})
    with session_scope() as db:
        from sales0.db.models import Enquiry

        assert db.get(Enquiry, eid).actor_user_id == _member_id("meera@acct.example")
    assert 'id="acting-select"' not in member.get("/inbox").text


def test_admin_acting_as_records_both_users(admin: TestClient, make_client: Callable[[], TestClient]) -> None:
    add_member(admin, "Meera Member", "meera@acct.example")
    mid, boss = _member_id("meera@acct.example"), _member_id(ADMIN_EMAIL)
    assert 'id="acting-select"' in admin.get("/inbox").text
    assert admin.post("/team/acting", data={"member_id": str(mid), "next": "/inbox"}).status_code == 303
    eid = _seed_review_enquiry()
    admin.post("/inbox/decide", data={"enquiry_ids": str(eid), "decision": "accept"})
    with session_scope() as db:
        from sales0.db.models import Enquiry

        e = db.get(Enquiry, eid)
        assert e.actor_user_id == mid  # credit goes to the member acted as
        h = db.scalars(select(StatusHistory).where(StatusHistory.entity_id == eid, StatusHistory.field == "review_status")).one()
        assert "by Boss Admin" in (h.note or "")
        ev = db.scalars(select(TimelineEvent).where(TimelineEvent.prospect_id == e.prospect_id)).all()
        assert any(t.actor == "Boss Admin as Meera Member" and t.actor_member_id == mid and (t.data or {}).get("by_member_id") == boss for t in ev)
    # admin rights stay the real user's: still can open Users while acting as a plain member
    assert admin.get("/users").status_code == 200


# ---------------------------------------------------------------------- open mode
def test_open_mode_banner_and_no_login(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SALES0_APP_PASSWORD", raising=False)
    from sales0.config import reset_settings

    reset_settings()
    with TestClient(create_app(), follow_redirects=False) as c:
        page = c.get("/inbox")
        assert page.status_code == 200
        assert 'id="open-mode-banner"' in page.text and "Open mode - anyone who can reach this app can use it" in page.text
        assert 'id="logout"' not in page.text and 'id="acting-select"' in page.text
        assert c.get("/login").status_code == 303
        assert c.get("/users").status_code == 200


def test_open_mode_ends_when_the_first_password_is_set(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SALES0_APP_PASSWORD", raising=False)
    from sales0.config import reset_settings

    reset_settings()
    with TestClient(create_app(), follow_redirects=False) as c:
        r = c.get("/users")
        admin_id = _member_id("admin@smalltech.in")
        r = c.post(f"/users/{admin_id}/reset")
        link = "/set-password/" + LINK.search(r.text).group(1).rsplit("/set-password/", 1)[1]
        assert c.post(link, data={"password": ADMIN_PASSWORD, "confirm": ADMIN_PASSWORD}).status_code == 303
        assert c.get("/inbox").status_code == 303  # now closed
        assert login(c, "admin@smalltech.in", ADMIN_PASSWORD).status_code == 303
        assert c.get("/inbox").status_code == 200


def test_startup_refuses_public_host_in_open_mode_but_not_with_account_passwords(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from sales0.config import StartupError, check_startup, get_settings

    monkeypatch.delenv("SALES0_APP_PASSWORD", raising=False)
    monkeypatch.setenv("SALES0_ANTHROPIC_API_KEY", "k-test")
    from sales0.config import reset_settings

    reset_settings()
    public = get_settings().model_copy(update={"host": "0.0.0.0"})
    with pytest.raises(StartupError, match="open mode"):
        check_startup(public)
    monkeypatch.setenv("SALES0_APP_PASSWORD", ADMIN_PASSWORD)
    reset_settings()
    check_startup(get_settings().model_copy(update={"host": "0.0.0.0"}))  # bootstrap password counts
    monkeypatch.delenv("SALES0_APP_PASSWORD")
    reset_settings()
    with TestClient(create_app(), follow_redirects=False):
        with session_scope() as db:
            from sales0.services.team import accounts as acct

            m = acct.members_service.seed_admin(db)
            m.password_hash = "$argon2id$stub"  # any stored password means accounts are on
    check_startup(get_settings().model_copy(update={"host": "0.0.0.0"}))
