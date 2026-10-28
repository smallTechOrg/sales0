"""Operator CLI: ``uv run python -m sales0.cli check-llm`` and ``... check-browser``."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sales0.config import Settings
    from sales0.scraper.browser import BrowserChoice

# CLI ``type=`` labels (architecture.md -> LLM layer). Most specific class first: a
# workspace-required error is also an ``LLMAuthError``.
_ERROR_LABELS: tuple[tuple[str, str], ...] = (
    ("LLMWorkspaceRequired", "workspace-required"),
    ("LLMAuthError", "auth"),
    ("LLMModelNotFound", "model-not-found"),
    ("LLMTransientError", "transient"),
    ("LLMBadResponse", "bad-response"),
)


def error_label(exc: Exception) -> str:
    """The ``LLM ERROR type=<label>`` value for an adapter error."""
    from sales0.llm import errors

    for class_name, label in _ERROR_LABELS:
        if isinstance(exc, getattr(errors, class_name)):
            return label
    return "bad-response"


def _check_llm() -> int:
    from pydantic import ValidationError

    from sales0.config import get_settings

    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"LLM ERROR invalid configuration: {exc.error_count()} error(s) — see .env.example", file=sys.stderr)
        return 1

    from sales0.llm import LLMError
    from sales0.llm.anthropic_adapter import AnthropicClient
    from sales0.logging import configure_logging

    # Logs go to stderr so stdout carries only the result line.
    configure_logging(settings, stream=sys.stderr)
    try:
        response = AnthropicClient(settings).ping()
    except LLMError as exc:
        print(f"LLM ERROR type={error_label(exc)} {exc.message}", file=sys.stderr)
        return 1
    print(
        f"LLM OK model={settings.llm_model} "
        f"input_tokens={response.input_tokens} output_tokens={response.output_tokens}"
    )
    return 0


# ----------------------------------------------------------------------------- check-browser
CHECK_BROWSER_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class BrowserProbe:
    """What a headless launch on a throwaway profile reports (about:blank only)."""

    product: str  # CDP Browser.getVersion ``product``, e.g. "Chrome/156.0.8078.48"
    user_agent: str  # navigator.userAgent
    webdriver: bool  # navigator.webdriver
    page_url: str  # the only page's URL (always about:blank)


def probe_browser(settings: Settings, choice: BrowserChoice) -> BrowserProbe:
    """Launch ``choice`` HEADLESS on a temp profile (never ``data/``), read the version over
    CDP plus the UA and webdriver flag on ``about:blank``, close, delete the profile.

    Never navigates: the persistent context's initial ``about:blank`` tab is the only page.
    Raises ``BrowserUnavailable`` when the browser cannot start.
    """
    from sales0.scraper.browser import first_page, launch_options, launch_persistent

    with tempfile.TemporaryDirectory(prefix="sales0-check-browser-") as tmp:
        options = launch_options(settings, choice, headless=True)
        options["user_data_dir"] = tmp
        with launch_persistent(options, choice) as context:
            page = first_page(context)
            cdp = context.new_cdp_session(page)
            try:
                product = str(cdp.send("Browser.getVersion").get("product", ""))
            finally:
                cdp.detach()
            user_agent = str(page.evaluate("() => navigator.userAgent"))
            webdriver = bool(page.evaluate("() => navigator.webdriver === true"))
            return BrowserProbe(product=product, user_agent=user_agent, webdriver=webdriver, page_url=page.url)


def version_from_product(product: str) -> str:
    """``Chrome/156.0.8078.48`` (or ``HeadlessChrome/…``) -> ``156.0.8078.48``."""
    return product.rsplit("/", 1)[-1].strip() if "/" in product else product.strip()


def automation_tell(probe: BrowserProbe) -> str | None:
    """Why the browser looks automated, or None."""
    if probe.webdriver:
        return "navigator.webdriver is true"
    if "HeadlessChrome" in probe.user_agent:
        return "the User-Agent contains HeadlessChrome"
    return None


def _kill_descendants(pid: int) -> None:
    """SIGKILL every descendant of ``pid`` (the Playwright driver and the browser it spawned)."""
    try:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return
    for line in out.split():
        if line.isdigit():
            child = int(line)
            _kill_descendants(child)
            try:
                os.kill(child, signal.SIGKILL)
            except OSError:
                pass


class Watchdog:
    """Calls ``on_fire`` once if not cancelled within ``timeout_s`` (a daemon timer thread)."""

    def __init__(self, timeout_s: float, on_fire: Callable[[], None]) -> None:
        self.fired = False
        self._on_fire = on_fire
        self._timer = threading.Timer(timeout_s, self._fire)
        self._timer.daemon = True

    def _fire(self) -> None:
        self.fired = True
        self._on_fire()

    def __enter__(self) -> Watchdog:
        self._timer.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._timer.cancel()


def _timeout_exit(timeout_s: float) -> None:
    print(
        f"browser ERROR type=timeout the browser did not answer within {timeout_s:.0f} s — "
        "quit any sales0 browser window and try again",
        file=sys.stderr,
        flush=True,
    )
    _kill_descendants(os.getpid())
    os._exit(1)


def check_browser(
    settings: Settings,
    *,
    probe: Callable[[Settings, BrowserChoice], BrowserProbe] | None = None,
    timeout_s: float = CHECK_BROWSER_TIMEOUT_S,
    on_timeout: Callable[[], None] | None = None,
) -> int:
    """Resolve the browser and smoke-launch it headless; print ONE result line."""
    from sales0.scraper.browser import resolve_browser
    from sales0.scraper.errors import BrowserUnavailable

    run_probe = probe or probe_browser
    with Watchdog(timeout_s, on_timeout or (lambda: _timeout_exit(timeout_s))):
        try:
            choice = resolve_browser(settings)
        except BrowserUnavailable as exc:
            print(f"browser ERROR type=chrome-not-found {exc}", file=sys.stderr)
            return 1
        try:
            result = run_probe(settings, choice)
        except Exception as exc:  # noqa: BLE001 — every launch failure is reported, not raised
            message = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            print(f"browser ERROR type=launch-failed {message}", file=sys.stderr)
            return 1
    tell = automation_tell(result)
    if tell is not None:
        print(f"browser ERROR type=automation-tell {tell} (channel={choice.channel})", file=sys.stderr)
        return 1
    line = f"browser OK channel={choice.channel} version={version_from_product(result.product) or choice.version}"
    if choice.note:
        line += f" ({choice.note})"
    print(line)
    return 0


def _check_browser() -> int:
    from pydantic import ValidationError

    from sales0.config import get_settings

    try:
        settings = get_settings()
    except ValidationError as exc:
        print(
            f"browser ERROR type=launch-failed invalid configuration: {exc.error_count()} error(s) — see .env.example",
            file=sys.stderr,
        )
        return 1

    from sales0.logging import configure_logging

    configure_logging(settings, stream=sys.stderr)  # stdout carries only the result line
    return check_browser(settings)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sales0.cli", description="sales0 operator commands")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-llm", help="One real Haiku call to prove the key and model slug are alive")
    sub.add_parser(
        "check-browser",
        help="Launch the configured browser headless on a throwaway profile (about:blank only) and report it",
    )
    sub.add_parser("google-auth", help="Sign in to Google (browser consent) and store the Gmail token")
    sub.add_parser("gmail-check", help="Draft to yourself with a PDF, fetch it, delete it; never sends")
    send = sub.add_parser("gmail-send-test", help="Send ONE labelled test email to your own address")
    send.add_argument("--yes-send", action="store_true", help="Required: actually send the test email")
    send.add_argument("--allow-external", action="store_true", help="dangerous")
    send.add_argument("--to", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command == "check-llm":
        return _check_llm()
    if args.command == "check-browser":
        return _check_browser()
    if args.command in ("google-auth", "gmail-check", "gmail-send-test"):
        from sales0.integrations.gmail.live import run_command

        return run_command(args)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
