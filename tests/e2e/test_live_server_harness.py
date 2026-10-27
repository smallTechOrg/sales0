"""The E2E harness itself (architecture.md → E2E harness, Phase 1.1): it refuses the user's
port 8765, always hands the subprocess an explicit port, and carries the Phase 1.1 env
(cool-downs 0, bundled Chromium) unless a test overrides it. No server is started here."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.live_server import E2E_PORT, FORBIDDEN_PORT, LiveServer, LiveServerError


def test_harness_refuses_port_8765_outright(tmp_path: Path) -> None:
    with pytest.raises(LiveServerError, match="8765"):
        LiveServer(tmp_path, port=FORBIDDEN_PORT)
    with pytest.raises(LiveServerError, match="8765"):
        LiveServer(tmp_path, extra_env={"SALES0_PORT": "8765"})


def test_harness_env_pins_port_8766_and_phase11_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Whatever the parent process has (the root conftest pins a dead port; a stray .env or shell
    # value could say anything), the child gets the harness's own values.
    monkeypatch.setenv("SALES0_PORT", "8765")
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "60")
    monkeypatch.setenv("SALES0_DATABASE_URL", "sqlite:///data/sales0.db")
    env = LiveServer(tmp_path).env()
    assert E2E_PORT == 8766
    assert env["SALES0_PORT"] == "8766"
    assert env["SALES0_LINKEDIN_MODE"] == "fixture"
    assert env["SALES0_SAFETY_COOLDOWN_MIN"] == "0"
    assert env["SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN"] == "0"
    assert env["SALES0_LINKEDIN_BROWSER_CHANNEL"] == "chromium"
    assert env["SALES0_DATABASE_URL"].startswith("sqlite:///") and str(tmp_path) in env["SALES0_DATABASE_URL"]
    assert str(tmp_path) in env["SALES0_DATA_DIR"]
    assert "SALES0_ANTHROPIC_API_KEY" not in env  # the child reads .env itself
    assert "SALES0_LINKEDIN_HEADLESS" not in env  # removed in Phase 1.1


def test_harness_extra_env_overrides_phase11_defaults(tmp_path: Path) -> None:
    env = LiveServer(tmp_path, extra_env={"SALES0_SAFETY_COOLDOWN_MIN": "60"}).env()
    assert env["SALES0_SAFETY_COOLDOWN_MIN"] == "60"
    assert env["SALES0_PORT"] == "8766"
