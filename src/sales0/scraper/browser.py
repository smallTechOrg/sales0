"""Browser resolution, profiles and launch options for the LinkedIn browser (Phase 1.1).

sales0 drives the user's installed **Google Chrome** (Playwright ``channel="chrome"``) on a
dedicated sales0 profile under the gitignored data dir, or the bundled Chromium when Chrome
is not installed (``SALES0_LINKEDIN_BROWSER_CHANNEL=auto``). Connect is the only headed
launch; every check and run is headless on the same profile with the identical argument set
(architecture.md -> ``## LinkedIn browser (Phase 1.1)``).

Automation flags (AUDIT_SCRAPER.md #14, decision): ``--disable-blink-features=AutomationControlled`` and the
removal of Playwright's ``--enable-automation`` are KEPT. A less-evasive default (webdriver=true, the
"controlled by automated software" infobar on the headed Connect) was weighed and rejected for now because
the repo's own contract tests it: ``sales0 check-browser`` fails on ``navigator.webdriver`` being true
(``cli.automation_tell`` + ``tests/integration/test_check_browser_cli.py``) and the options are pinned in
``tests/unit/scraper/test_browser_options.py``. Residual risk: LinkedIn can still fingerprint headless Chrome
by other means (the in-page clipboard override in ``permalink.py``, uniform timing); behavioural mitigations
(eased multi-step scrolls, jittered waits, small daily page-load budgets) live in ``search.py`` / ``pacing.py``.

Resolution never starts a browser: Chrome's version comes from its ``Info.plist`` (macOS) or
``--version`` (Linux), Chromium's from Playwright's bundled ``browsers.json``.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from playwright.sync_api import BrowserContext, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from sales0.config import Settings
from sales0.scraper.pacing import scaled_ms
from sales0.logging import get_logger
from sales0.scraper.errors import BrowserUnavailable

if TYPE_CHECKING:
    from sales0.scraper.session_store import AccountSpec

log = get_logger("sales0.scraper.browser")

Channel = Literal["chrome", "chromium"]

# --------------------------------------------------------------------------- constants
MAC_CHROME_APP = Path("/Applications/Google Chrome.app")
MAC_CHROME_BINARY_IN_APP = Path("Contents/MacOS/Google Chrome")
LINUX_CHROME_BINARY = Path("/opt/google/chrome/chrome")

CHROME_LABEL = "Google Chrome"
CHROMIUM_LABEL = "Built-in browser (Chromium)"
FALLBACK_NOTE = "Google Chrome not found — using the built-in browser"
CHROME_MISSING_MESSAGE = (
    "Google Chrome is not installed — install it from https://www.google.com/chrome/ "
    "or set SALES0_LINKEDIN_BROWSER_CHANNEL=auto"
)
PROFILE_IN_USE_MESSAGE = "The sales0 browser window is still open — close it and try again"

# Profile directory per (mode, channel); a Chrome and a Chromium profile are never shared.
PROFILE_DIRNAMES: dict[tuple[str, str], str] = {
    ("live", "chrome"): "linkedin-chrome-profile",
    ("live", "chromium"): "linkedin-profile",
    ("fixture", "chrome"): "linkedin-profile-fixture-chrome",
    ("fixture", "chromium"): "linkedin-profile-fixture-chromium",
}

WINDOW_WIDTH, WINDOW_HEIGHT = 1280, 860
BASE_ARGS: tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
)
HEADED_ONLY_ARGS: tuple[str, ...] = (f"--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}",)
# Only this Playwright default arg is removed. ``--use-mock-keychain`` and
# ``--password-store=basic`` stay, because they decide how the profile's cookies are
# encrypted and must match between the headed Connect and the headless runs.
IGNORED_DEFAULT_ARGS: tuple[str, ...] = ("--enable-automation",)

# Chrome's frozen platform tokens in the reduced User-Agent.
_UA_PLATFORMS: dict[str, str] = {
    "darwin": "Macintosh; Intel Mac OS X 10_15_7",
    "linux": "X11; Linux x86_64",
    "win32": "Windows NT 10.0; Win64; x64",
}
_DEFAULT_LOCALE = "en-US"
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


# --------------------------------------------------------------------------- resolution
@dataclass(frozen=True)
class ChromeInstall:
    """A Google Chrome binary at one of Playwright's standard channel locations."""

    binary: Path
    app: Path | None = None  # macOS ``.app`` bundle (version comes from its Info.plist)
    # Playwright looks only in /Applications; a ~/Applications install needs executable_path.
    needs_executable_path: bool = False


@dataclass(frozen=True)
class BrowserChoice:
    channel: Channel
    version: str | None  # e.g. "156.0.8078.48"; None if unknown
    label: str  # "Google Chrome 156" | "Built-in browser (Chromium)"
    note: str | None  # FALLBACK_NOTE only on an auto fallback
    executable_path: str | None = None  # set instead of ``channel`` for a ~/Applications Chrome


def find_chrome(platform: str = sys.platform, home: Path | None = None) -> ChromeInstall | None:
    """The installed Google Chrome, or None. Never starts a browser."""
    if platform == "darwin":
        system_app = MAC_CHROME_APP
        if (system_app / MAC_CHROME_BINARY_IN_APP).is_file():
            return ChromeInstall(binary=system_app / MAC_CHROME_BINARY_IN_APP, app=system_app)
        user_app = (home or Path.home()) / "Applications" / "Google Chrome.app"
        if (user_app / MAC_CHROME_BINARY_IN_APP).is_file():
            return ChromeInstall(binary=user_app / MAC_CHROME_BINARY_IN_APP, app=user_app, needs_executable_path=True)
        return None
    if platform.startswith("linux"):
        return ChromeInstall(binary=LINUX_CHROME_BINARY) if LINUX_CHROME_BINARY.is_file() else None
    return None


_VERSION_RE = re.compile(r"(\d+(?:\.\d+){1,3})")


def chrome_version(install: ChromeInstall) -> str | None:
    """Chrome's version from ``Info.plist`` (macOS) or ``<binary> --version`` (Linux, 5 s)."""
    if install.app is not None:
        try:
            with (install.app / "Contents" / "Info.plist").open("rb") as fh:
                value = plistlib.load(fh).get("CFBundleShortVersionString")
        except (OSError, plistlib.InvalidFileException, ValueError):
            return None
        text = str(value).strip() if value else ""
        return text or None
    try:
        out = subprocess.run(
            [str(install.binary), "--version"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search(out or "")
    return match.group(1) if match else None


def _playwright_browsers_json() -> Path:
    import playwright

    return Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"


def chromium_version(browsers_json: Path | None = None) -> str | None:
    """``browserVersion`` of the bundled ``chromium`` entry in Playwright's ``browsers.json``."""
    path = browsers_json or _playwright_browsers_json()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in data.get("browsers", []):
            if entry.get("name") == "chromium":
                version = entry.get("browserVersion")
                return str(version) if version else None
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    return None


def _major(version: str | None) -> str | None:
    if not version:
        return None
    head = version.split(".", 1)[0]
    return head if head.isdigit() else None


def _chrome_choice(install: ChromeInstall) -> BrowserChoice:
    version = chrome_version(install)
    major = _major(version)
    return BrowserChoice(
        channel="chrome",
        version=version,
        label=f"{CHROME_LABEL} {major}" if major else CHROME_LABEL,
        note=None,
        executable_path=str(install.binary) if install.needs_executable_path else None,
    )


def _chromium_choice(note: str | None) -> BrowserChoice:
    return BrowserChoice(channel="chromium", version=chromium_version(), label=CHROMIUM_LABEL, note=note)


_cache: dict[str, BrowserChoice] = {}
_cache_lock = threading.Lock()


def resolve_browser(settings: Settings) -> BrowserChoice:
    """Which browser sales0 drives (cached per process and channel setting).

    Raises ``BrowserUnavailable`` for ``SALES0_LINKEDIN_BROWSER_CHANNEL=chrome`` when Google
    Chrome is not installed (not cached, so installing Chrome is picked up).
    """
    requested = settings.linkedin_browser_channel
    with _cache_lock:
        cached = _cache.get(requested)
    if cached is not None:
        return cached
    if requested == "chromium":
        choice = _chromium_choice(note=None)
    else:
        install = find_chrome()
        if install is not None:
            choice = _chrome_choice(install)
        elif requested == "chrome":
            raise BrowserUnavailable(CHROME_MISSING_MESSAGE)
        else:
            choice = _chromium_choice(note=FALLBACK_NOTE)
    with _cache_lock:
        _cache[requested] = choice
    return choice


def reset_browser_cache() -> None:
    """Test seam: forget the resolved browser and the system locale."""
    with _cache_lock:
        _cache.clear()
    _clear_locale_cache()


# --------------------------------------------------------------------------- profiles
def profile_dir(settings: Settings, channel: str, account: AccountSpec | None = None) -> Path:
    """The dedicated sales0 profile for this mode and channel, under ``settings.data_path``.

    The default account (``profile_dirname`` unset or one of the legacy ``PROFILE_DIRNAMES``) keeps the
    pre-p8 directory. Any other folder account gets its own ``<profile_dirname>-<mode>-<channel>`` directory.
    """
    custom = account.profile_dirname if account is not None else None
    if custom and custom not in PROFILE_DIRNAMES.values():
        if channel not in ("chrome", "chromium"):
            raise ValueError(f"Unknown browser channel {channel!r}; use 'chrome' or 'chromium'")
        return settings.data_path / f"{custom}-{settings.linkedin_mode}-{channel}"
    try:
        name = PROFILE_DIRNAMES[(settings.linkedin_mode, channel)]
    except KeyError:
        raise ValueError(f"Unknown browser channel {channel!r}; use 'chrome' or 'chromium'") from None
    return settings.data_path / name


def profile_in_use(profile: Path) -> bool:
    """True when a live process holds the profile's Chrome ``SingletonLock``.

    The lock is a symlink to ``<hostname>-<pid>``. A stale lock (dead pid) or one from
    another host is left for Chrome to handle.
    """
    lock = profile / "SingletonLock"
    if not lock.is_symlink():
        return False
    try:
        target = os.readlink(lock)
    except OSError:
        return False
    host, _, pid_text = target.rpartition("-")
    if not pid_text.isdigit():
        return False
    if host and host != socket.gethostname():
        return False
    try:
        os.kill(int(pid_text), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- launch options
def user_agent_for(version: str, platform: str = sys.platform) -> str:
    """Chrome's reduced User-Agent for ``version`` (minor parts frozen to 0.0.0)."""
    major = _major(version) or version.split(".", 1)[0]
    if platform.startswith("linux"):
        token = _UA_PLATFORMS["linux"]
    elif platform.startswith("win"):
        token = _UA_PLATFORMS["win32"]
    else:
        token = _UA_PLATFORMS["darwin"]
    return f"Mozilla/5.0 ({token}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"


def normalize_locale(raw: str | None) -> str | None:
    """``en_IN`` / ``en_IN.UTF-8`` / ``en_US@rg=inzzzz`` -> ``en-IN`` / ``en-US``; junk -> None."""
    if not raw:
        return None
    value = raw.strip().split("@", 1)[0].split(".", 1)[0].replace("_", "-")
    if value in ("C", "POSIX") or not _LOCALE_RE.match(value):
        return None
    parts = value.split("-")
    parts[0] = parts[0].lower()
    if len(parts) > 1 and len(parts[-1]) == 2:
        parts[-1] = parts[-1].upper()
    return "-".join(parts)


@lru_cache(maxsize=1)
def system_locale() -> str:
    """The OS locale as BCP-47 (macOS ``AppleLocale``, then ``LC_ALL``/``LANG``), else en-US."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleLocale"], capture_output=True, text=True, timeout=2, check=False
            )
            found = normalize_locale(out.stdout) if out.returncode == 0 else None
            if found:
                return found
        except (OSError, subprocess.SubprocessError):
            pass
    for var in ("LC_ALL", "LANG"):
        found = normalize_locale(os.environ.get(var))
        if found:
            return found
    return _DEFAULT_LOCALE


# Captured at import so the reset seam still clears the real cache while a test stubs it.
_clear_locale_cache = system_locale.cache_clear


def launch_options(
    settings: Settings, choice: BrowserChoice, *, headless: bool, account: AccountSpec | None = None
) -> dict[str, Any]:
    """Keyword args for ``launch_persistent_context`` (pure apart from the cached locale).

    ``account`` overrides the profile directory and any pinned user agent / locale / timezone / viewport.
    """
    args = list(BASE_ARGS) if headless else [*BASE_ARGS, *HEADED_ONLY_ARGS]
    options: dict[str, Any] = {
        "user_data_dir": str(profile_dir(settings, choice.channel, account)),
        "headless": headless,
        "ignore_default_args": list(IGNORED_DEFAULT_ARGS),
        "args": args,
        "chromium_sandbox": True,
        "locale": system_locale(),
        "timezone_id": settings.timezone,
    }
    if choice.executable_path:
        options["executable_path"] = choice.executable_path
    else:
        options["channel"] = choice.channel
    if headless:
        options["viewport"] = {"width": WINDOW_WIDTH, "height": WINDOW_HEIGHT}
        if choice.version:
            options["user_agent"] = user_agent_for(choice.version)
    else:
        options["no_viewport"] = True
    if account is not None:
        if account.user_agent:
            options["user_agent"] = account.user_agent
        if account.locale:
            options["locale"] = account.locale
        if account.timezone:
            options["timezone_id"] = account.timezone
        if account.viewport:
            options.pop("no_viewport", None)
            options["viewport"] = {"width": account.viewport[0], "height": account.viewport[1]}
        if (account.proxy_url) and account.backend == "folder":
            from sales0.scraper.session_store import proxy_options

            options["proxy"] = proxy_options(account.proxy_url)
    return options


def _short_error(exc: BaseException, limit: int = 200) -> str:
    text = str(exc).strip().splitlines()
    first = text[0] if text else type(exc).__name__
    return first if len(first) <= limit else first[: limit - 1] + "…"


def launch_failed_message(choice: BrowserChoice, exc: BaseException) -> str:
    name = CHROME_LABEL if choice.channel == "chrome" else "the built-in browser"
    window = "sales0 Chrome window" if choice.channel == "chrome" else "sales0 browser window"
    return f"Could not start {name} ({_short_error(exc)}) — quit any {window} and try again"


@contextmanager
def launch_persistent(options: dict[str, Any], choice: BrowserChoice) -> Iterator[BrowserContext]:
    """Start Playwright, launch with ``options``, and always close the context and driver.

    Every launch ``PlaywrightError`` becomes ``BrowserUnavailable`` with a next step.
    """
    pw = sync_playwright().start()
    try:
        try:
            context = pw.chromium.launch_persistent_context(**options)
        except PlaywrightError as exc:
            raise BrowserUnavailable(launch_failed_message(choice, exc)) from exc
        try:
            yield context
        finally:
            try:
                context.close()
            except PlaywrightError:
                pass  # the user may already have closed the window
    finally:
        pw.stop()


@contextmanager
def open_context(
    settings: Settings, *, headless: bool, account: AccountSpec | None = None
) -> Iterator[BrowserContext]:
    """Resolve the browser, open (and always close) a persistent context on its sales0 profile.

    Raises ``BrowserUnavailable`` when Chrome is required but missing, when the profile is
    held by a still-open sales0 window, or when the browser fails to launch.
    """
    choice = resolve_browser(settings)
    options = launch_options(settings, choice, headless=headless, account=account)
    profile = Path(options["user_data_dir"])
    profile.mkdir(parents=True, exist_ok=True)
    if profile_in_use(profile):
        raise BrowserUnavailable(PROFILE_IN_USE_MESSAGE)
    overridden = "user_agent" in options
    if headless and not overridden:
        log.warning("headless_ua_unmasked", channel=choice.channel, profile=profile.name)
    log.debug(
        "browser_launch",
        channel=choice.channel,
        headless=headless,
        profile=profile.name,
        user_agent_overridden=overridden,
    )
    with launch_persistent(options, choice) as context:
        yield context


def first_page(context: BrowserContext) -> Page:
    """The profile's initial tab (persistent contexts open with one), or a new one."""
    for page in context.pages:
        if not page.is_closed():
            return page
    return context.new_page()


# --------------------------------------------------------------------------- timing
def wait_timeout_ms(settings: Settings) -> int:
    """How long to wait for page content to render (LinkedIn is a slow SPA; the fixture is not)."""
    return 5_000 if settings.linkedin_mode == "fixture" else 15_000


def scroll_settle_ms(settings: Settings) -> int:
    """How long to wait for new result cards after a scroll before concluding the end."""
    base = 1_200 if settings.linkedin_mode == "fixture" else 2_500
    return scaled_ms(settings, base)
