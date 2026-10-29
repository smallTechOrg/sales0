"""SessionStore pieces that need no browser: proxy parsing, per-account profile directories, pinned launch options."""

from __future__ import annotations

from pathlib import Path

import pytest

from sales0.config import Settings
from sales0.scraper.browser import BrowserChoice, launch_options, profile_dir
from sales0.scraper.errors import BrowserUnavailable
from sales0.scraper.session_store import (
    AccountHandle,
    AccountSpec,
    DbSessionStore,
    FolderSessionStore,
    _parse_state,
    proxy_options,
    store_for,
)

CHROMIUM = BrowserChoice(channel="chromium", version="140.0.1.2", label="Built-in browser (Chromium)", note=None)


def test_proxy_options_split_credentials_from_the_server() -> None:
    assert proxy_options(None) is None and proxy_options("") is None
    assert proxy_options("http://proxy.example:8080") == {"server": "http://proxy.example:8080"}
    assert proxy_options("socks5://u%40x:p%3Aw@10.0.0.1:1080") == {
        "server": "socks5://10.0.0.1:1080", "username": "u@x", "password": "p:w",
    }
    with pytest.raises(BrowserUnavailable):
        proxy_options("not-a-proxy")


def test_the_default_account_keeps_the_legacy_profile_directories(tmp_path: Path) -> None:
    settings = Settings(data_dir=str(tmp_path), linkedin_mode="live")
    legacy = AccountSpec(id=1, profile_dirname="linkedin-chrome-profile")
    assert profile_dir(settings, "chrome", legacy) == profile_dir(settings, "chrome") == tmp_path / "linkedin-chrome-profile"
    # a legacy name resolves by the CURRENT channel, exactly as before p8
    assert profile_dir(settings, "chromium", legacy) == tmp_path / "linkedin-profile"
    assert profile_dir(settings, "chrome", AccountSpec(id=1)) == tmp_path / "linkedin-chrome-profile"


def test_other_folder_accounts_get_their_own_directory_per_mode_and_channel(tmp_path: Path) -> None:
    other = AccountSpec(id=2, profile_dirname="linkedin-account-2")
    live = Settings(data_dir=str(tmp_path), linkedin_mode="live")
    fixture = Settings(data_dir=str(tmp_path), linkedin_mode="fixture")
    assert profile_dir(live, "chrome", other) == tmp_path / "linkedin-account-2-live-chrome"
    assert profile_dir(fixture, "chromium", other) == tmp_path / "linkedin-account-2-fixture-chromium"
    with pytest.raises(ValueError):
        profile_dir(live, "firefox", other)


def test_pins_override_the_launch_options_and_unpinned_accounts_do_not(tmp_path: Path) -> None:
    settings = Settings(data_dir=str(tmp_path), linkedin_mode="live")
    plain = launch_options(settings, CHROMIUM, headless=True)
    assert launch_options(settings, CHROMIUM, headless=True, account=AccountSpec(id=1)) == plain
    pinned = launch_options(
        settings, CHROMIUM, headless=True,
        account=AccountSpec(id=3, profile_dirname="linkedin-account-3", user_agent="UA/1", locale="fr-FR",
                            timezone="Europe/Paris", viewport=(1000, 700), proxy_url="http://p.example:3128"),
    )
    assert pinned["user_agent"] == "UA/1" and pinned["locale"] == "fr-FR" and pinned["timezone_id"] == "Europe/Paris"
    assert pinned["viewport"] == {"width": 1000, "height": 700}
    assert pinned["proxy"] == {"server": "http://p.example:3128"}
    assert pinned["user_data_dir"].endswith("linkedin-account-3-live-chromium")


def test_a_db_account_takes_its_proxy_at_context_level_not_in_the_persistent_options(tmp_path: Path) -> None:
    settings = Settings(data_dir=str(tmp_path), linkedin_mode="live")
    options = launch_options(settings, CHROMIUM, headless=True, account=AccountSpec(id=4, backend="db", proxy_url="http://p:1"))
    assert "proxy" not in options


def test_store_for_picks_the_backend_and_state_parsing_is_forgiving() -> None:
    assert isinstance(store_for(AccountHandle(AccountSpec(id=1))), FolderSessionStore)
    assert isinstance(store_for(AccountHandle(AccountSpec(id=2, backend="db"))), DbSessionStore)
    assert _parse_state(None) == {"cookies": [], "origins": []}
    assert _parse_state("not json") == {"cookies": [], "origins": []}
    assert _parse_state("[1]") == {"cookies": [], "origins": []}
    assert _parse_state('{"cookies": [{"name": "li_at"}], "origins": []}')["cookies"][0]["name"] == "li_at"
