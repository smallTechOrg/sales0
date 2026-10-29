"""Accounts fixtures: an app with a bootstrapped admin, helpers to log members in via set-password links."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from sales0.config import reset_settings
from sales0.web.app import create_app

ADMIN_EMAIL = "boss@acct.example"
ADMIN_PASSWORD = "admin-pass-1234"
MEMBER_PASSWORD = "member-pass-5678"
LINK = re.compile(r'id="set-password-url"[^>]*value="([^"]+)"')


@pytest.fixture
def env(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_APP_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("SALES0_ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("SALES0_ADMIN_NAME", "Boss Admin")
    monkeypatch.setenv("SALES0_SECRET_KEY", "accounts-test-secret")
    reset_settings()


@pytest.fixture
def make_client(env: None) -> Iterator[Callable[[], TestClient]]:
    opened: list[TestClient] = []

    def _make() -> TestClient:
        c = TestClient(create_app(), follow_redirects=False)
        c.__enter__()
        opened.append(c)
        return c

    yield _make
    for c in opened:
        c.__exit__(None, None, None)
    reset_settings()


def login(c: TestClient, email: str, password: str, **extra: str):
    return c.post("/login", data={"email": email, "password": password, **extra})


@pytest.fixture
def admin(make_client: Callable[[], TestClient]) -> TestClient:
    c = make_client()
    assert login(c, ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 303
    return c


def add_member(admin_client: TestClient, name: str, email: str, role: str = "member") -> str:
    """Add via the Users page; returns the one-time link path (/set-password/<token>)."""
    r = admin_client.post("/users", data={"name": name, "email": email, "role": role})
    assert r.status_code == 200, r.text
    m = LINK.search(r.text)
    assert m, "the set-password link must be shown on the response"
    return "/set-password/" + m.group(1).rsplit("/set-password/", 1)[1]


def activate(make_client: Callable[[], TestClient], link: str, email: str, password: str = MEMBER_PASSWORD) -> TestClient:
    """A fresh browser: open the link, choose a password, log in."""
    c = make_client()
    assert c.get(link).status_code == 200
    r = c.post(link, data={"password": password, "confirm": password})
    assert r.status_code == 303 and r.headers["location"] == "/login", r.text
    assert login(c, email, password).status_code == 303
    return c
