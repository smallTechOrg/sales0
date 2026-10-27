"""Phase 1.1 settings: browser channel, safety cool-downs, removed headless switch. Pure."""

from __future__ import annotations

import os
import re

import pytest
from pydantic import ValidationError

from sales0.config import REPO_ROOT, Settings


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("SALES0_"):
            monkeypatch.delenv(key)


def test_phase_1_1_defaults_are_auto_chrome_and_60_15_minute_cool_downs(clean_env: None) -> None:
    s = Settings(_env_file=None)
    assert s.linkedin_browser_channel == "auto"
    assert (s.safety_cooldown_min, s.safety_cooldown_checkpoint_min) == (60, 15)
    assert s.linkedin_connect_timeout_s == 300


def test_the_headless_setting_no_longer_exists_and_a_stale_env_line_is_tolerated(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALES0_LINKEDIN_HEADLESS", "true")
    s = Settings(_env_file=None)
    assert not hasattr(s, "linkedin_headless")
    assert "linkedin_headless" not in Settings.model_fields


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("chrome", "chrome"), ("  Chromium  # tests only", "chromium"), ("AUTO", "auto"), ("chrome#x", "chrome")],
)
def test_browser_channel_strips_inline_comments_whitespace_and_case(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv("SALES0_LINKEDIN_BROWSER_CHANNEL", raw)
    assert Settings(_env_file=None).linkedin_browser_channel == expected


@pytest.mark.parametrize("raw", ["firefox", "edge", "chrome-beta"])
def test_an_unknown_browser_channel_fails_loudly(clean_env: None, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("SALES0_LINKEDIN_BROWSER_CHANNEL", raw)
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)
    assert "linkedin_browser_channel" in str(caught.value)


def test_an_empty_browser_channel_uses_the_default(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_LINKEDIN_BROWSER_CHANNEL", "")
    assert Settings(_env_file=None).linkedin_browser_channel == "auto"


def test_cool_downs_accept_zero_to_switch_them_off(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "0")
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN", "0")
    s = Settings(_env_file=None)
    assert (s.safety_cooldown_min, s.safety_cooldown_checkpoint_min) == (0, 0)


@pytest.mark.parametrize(
    ("var", "raw"),
    [
        ("SALES0_SAFETY_COOLDOWN_MIN", "-1"),
        ("SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN", "-5"),
        ("SALES0_SAFETY_COOLDOWN_MIN", "ten"),
        ("SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN", "1.5"),
    ],
)
def test_negative_or_non_integer_cool_downs_are_rejected(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, var: str, raw: str
) -> None:
    monkeypatch.setenv(var, raw)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_env_example_lists_the_phase_1_1_settings_in_the_linkedin_block_and_drops_headless() -> None:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SALES0_LINKEDIN_HEADLESS" not in text
    names = re.findall(r"^(SALES0_[A-Z0-9_]+)=", text, flags=re.MULTILINE)
    block = names[names.index("SALES0_LINKEDIN_MODE") : names.index("SALES0_LINKEDIN_MODE") + 5]
    assert block == [
        "SALES0_LINKEDIN_MODE",
        "SALES0_LINKEDIN_CONNECT_TIMEOUT_S",
        "SALES0_LINKEDIN_BROWSER_CHANNEL",
        "SALES0_SAFETY_COOLDOWN_MIN",
        "SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN",
    ]
    assert "SALES0_LINKEDIN_BROWSER_CHANNEL=auto" in text
    assert "SALES0_SAFETY_COOLDOWN_MIN=60" in text and "SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN=15" in text


def test_every_settings_field_is_documented_in_env_example() -> None:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^#?\s*(SALES0_[A-Z0-9_]+)=", text, flags=re.MULTILINE))
    expected = {f"SALES0_{name.upper()}" for name in Settings.model_fields}
    assert expected <= documented, sorted(expected - documented)
