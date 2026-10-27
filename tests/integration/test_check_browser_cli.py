"""``python -m sales0.cli check-browser``: the real local browser smoke (part of the gate).

Runs the real command in a subprocess (HEADLESS, throwaway profile, about:blank only) and
the in-process probe with a request listener to prove nothing is ever navigated.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from sales0.cli import probe_browser
from sales0.config import REPO_ROOT, Settings, get_settings, reset_settings
from sales0.scraper import browser
from sales0.scraper.browser import find_chrome, resolve_browser

pytestmark = pytest.mark.browser

NOT_INSTALLED = "Google Chrome not installed — install it, or run this gate on the user's Mac"


def _run_cli(channel: str, data_dir: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SALES0_")}
    env.update(
        {
            "SALES0_LINKEDIN_BROWSER_CHANNEL": channel,
            # Proof the command never touches the sales0 profile: point the data dir at a
            # tmp dir and assert it stays empty.
            "SALES0_DATA_DIR": str(data_dir),
            "SALES0_PORT": os.environ["SALES0_PORT"],
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "sales0.cli", "check-browser"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )


def _installed_major() -> str:
    install = find_chrome()
    if install is None:
        pytest.fail(NOT_INSTALLED)
    version = browser.chrome_version(install)
    assert version
    return version.split(".")[0]


@pytest.mark.chrome
def test_check_browser_prints_exactly_one_ok_line_for_the_installed_chrome(tmp_path: Path) -> None:
    major = _installed_major()
    data_dir = tmp_path / "data"
    proc = _run_cli("auto", data_dir)
    assert proc.returncode == 0, proc.stderr
    assert re.fullmatch(rf"browser OK channel=chrome version={major}\.\d+\.\d+\.\d+\n", proc.stdout), proc.stdout
    assert not data_dir.exists() or not any(data_dir.iterdir())


@pytest.mark.chrome
def test_check_browser_with_channel_chrome_matches_auto(tmp_path: Path) -> None:
    _installed_major()
    proc = _run_cli("chrome", tmp_path / "data")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("browser OK channel=chrome version=")
    assert proc.stdout.count("\n") == 1


def test_check_browser_on_the_bundled_chromium_reports_chromium(tmp_path: Path) -> None:
    proc = _run_cli("chromium", tmp_path / "data")
    assert proc.returncode == 0, proc.stderr
    assert re.fullmatch(r"browser OK channel=chromium version=\d+\.\d+\.\d+\.\d+\n", proc.stdout), proc.stdout


def test_check_browser_with_an_invalid_channel_fails_on_stderr_only(tmp_path: Path) -> None:
    proc = _run_cli("firefox", tmp_path / "data")
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert proc.stderr.strip().splitlines()[-1].startswith("browser ERROR type=launch-failed invalid configuration")


def test_check_browser_launch_failure_is_reported_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sales0.cli import check_browser
    from sales0.scraper.browser import BrowserChoice

    broken = BrowserChoice(
        channel="chrome",
        version="156.0.0.1",
        label="Google Chrome 156",
        note=None,
        executable_path=str(tmp_path / "no-such-chrome"),
    )
    monkeypatch.setattr(browser, "resolve_browser", lambda _s: broken)
    assert check_browser(Settings(data_dir=str(tmp_path))) == 1


def test_the_probe_opens_about_blank_only_and_never_the_sales0_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALES0_DATA_DIR", str(tmp_path / "data"))
    reset_settings()
    settings = get_settings()
    choice = resolve_browser(settings)  # chromium in the suite (tests/conftest.py)
    requests: list[str] = []

    from sales0.scraper import browser as browser_module

    original = browser_module.launch_persistent

    def recording(options, chosen):  # type: ignore[no-untyped-def]
        assert options["headless"] is True
        assert not Path(options["user_data_dir"]).is_relative_to(settings.data_path)
        assert not Path(options["user_data_dir"]).is_relative_to(REPO_ROOT / "data")
        cm = original(options, chosen)
        context = cm.__enter__()
        context.on("request", lambda req: requests.append(req.url))

        class _Wrap:
            def __enter__(self):  # type: ignore[no-untyped-def]
                return context

            def __exit__(self, *exc):  # type: ignore[no-untyped-def]
                return cm.__exit__(*exc)

        return _Wrap()

    monkeypatch.setattr(browser_module, "launch_persistent", recording)
    probe = probe_browser(settings, choice)
    assert probe.page_url == "about:blank"
    assert probe.webdriver is False and "HeadlessChrome" not in probe.user_agent
    assert probe.product
    assert requests == []
    assert not (tmp_path / "data").exists()
