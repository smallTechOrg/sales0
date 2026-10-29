"""SessionStore: where a LinkedIn account's logged-in browser state lives (Phase 5).

The scraper never touches the database. It receives an ``AccountHandle`` (a plain description of one
``linkedin_accounts`` row, built by ``services.linkedin_accounts.build_handle``) and asks the matching
store for a browser context:

* ``FolderSessionStore`` - today's behaviour: a persistent Chrome/Chromium profile directory under the
  data dir (the Connect window logs in once; every later check and run reuses the profile).
* ``DbSessionStore`` - Playwright ``storage_state`` (cookies + localStorage). The decrypted JSON arrives
  in the handle; the context is built with ``browser.new_context(storage_state=..., proxy, UA, locale,
  timezone, viewport)`` and, when the context closes, the refreshed state is handed back through
  ``handle.save_state`` (the service encrypts it with SALES0_SECRET_KEY before it reaches the row).

A handle of ``None`` means "the default folder profile exactly as before p8" and goes straight to
``browser.open_context``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from playwright.sync_api import BrowserContext
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from sales0.config import Settings
from sales0.logging import get_logger
from sales0.scraper import browser as browser_mod
from sales0.scraper.errors import BrowserUnavailable

log = get_logger("sales0.scraper.session_store")

# Cookies/origins of a state that has never logged in.
EMPTY_STATE: dict[str, Any] = {"cookies": [], "origins": []}


@dataclass(frozen=True)
class AccountSpec:
    """Everything the browser layer needs about one account (no secrets except ``storage_state``)."""

    id: int
    backend: str = "folder"  # folder | db
    profile_dirname: str | None = None
    storage_state: str | None = None  # DECRYPTED storage_state JSON (db backend); never logged
    proxy_url: str | None = None
    user_agent: str | None = None
    locale: str | None = None
    timezone: str | None = None
    viewport: tuple[int, int] | None = None


@dataclass
class AccountHandle:
    """An ``AccountSpec`` plus the callback that persists a refreshed ``storage_state`` JSON string."""

    spec: AccountSpec
    save_state: Callable[[str], None] | None = None

    @property
    def id(self) -> int:
        return self.spec.id

    @property
    def backend(self) -> str:
        return self.spec.backend


class SessionStore(Protocol):
    def open(self, settings: Settings, *, headless: bool) -> Any:
        """A context manager yielding a ``BrowserContext`` for this account."""


def proxy_options(proxy_url: str | None) -> dict[str, str] | None:
    """Playwright's ``proxy`` dict from ``scheme://[user:pass@]host:port`` (None for no proxy)."""
    if not proxy_url:
        return None
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(proxy_url)
    if not parts.scheme or not parts.hostname:
        raise BrowserUnavailable("The account's proxy URL is not valid (use scheme://host:port)")
    server = f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    options = {"server": server}
    if parts.username:
        options["username"] = unquote(parts.username)
    if parts.password:
        options["password"] = unquote(parts.password)
    return options


class FolderSessionStore:
    """A persistent browser profile on disk (the default, today's behaviour)."""

    def __init__(self, handle: AccountHandle) -> None:
        self.handle = handle

    @contextmanager
    def open(self, settings: Settings, *, headless: bool) -> Iterator[BrowserContext]:
        with browser_mod.open_context(settings, headless=headless, account=self.handle.spec) as context:
            yield context


class DbSessionStore:
    """``storage_state`` kept (encrypted) in the account row; a fresh incognito-style context per use."""

    def __init__(self, handle: AccountHandle) -> None:
        self.handle = handle

    @contextmanager
    def open(self, settings: Settings, *, headless: bool) -> Iterator[BrowserContext]:
        spec = self.handle.spec
        choice = browser_mod.resolve_browser(settings)
        launch = browser_mod.launch_options(settings, choice, headless=headless, account=spec)
        state = _parse_state(spec.storage_state)
        kwargs: dict[str, Any] = {"storage_state": state}
        for key in ("user_agent", "locale", "timezone_id", "viewport", "no_viewport"):
            if key in launch:
                kwargs[key] = launch[key]
        proxy = proxy_options(spec.proxy_url)
        if proxy:
            kwargs["proxy"] = proxy
        browser_args: dict[str, Any] = {
            "headless": headless,
            "args": launch["args"],
            "ignore_default_args": launch["ignore_default_args"],
            "chromium_sandbox": launch["chromium_sandbox"],
        }
        if "executable_path" in launch:
            browser_args["executable_path"] = launch["executable_path"]
        else:
            browser_args["channel"] = launch["channel"]
        log.debug("db_session_launch", account=spec.id, channel=choice.channel, headless=headless, proxy=bool(proxy))
        pw = sync_playwright().start()
        try:
            try:
                browser = pw.chromium.launch(**browser_args)
                context = browser.new_context(**kwargs)
            except PlaywrightError as exc:
                raise BrowserUnavailable(browser_mod.launch_failed_message(choice, exc)) from exc
            try:
                yield context
            finally:
                self._save(context)
                try:
                    context.close()
                    browser.close()
                except PlaywrightError:
                    pass
        finally:
            pw.stop()

    def _save(self, context: BrowserContext) -> None:
        if self.handle.save_state is None:
            return
        try:
            state = json.dumps(context.storage_state())
        except PlaywrightError:
            log.warning("db_session_state_unreadable", account=self.handle.id)
            return
        try:
            self.handle.save_state(state)
        except Exception:  # noqa: BLE001 - failing to persist state must not fail the run itself
            log.exception("db_session_state_save_failed", account=self.handle.id)


def _parse_state(raw: str | None) -> dict[str, Any]:
    if not raw:
        return dict(EMPTY_STATE)
    try:
        value = json.loads(raw)
    except ValueError:
        return dict(EMPTY_STATE)
    return value if isinstance(value, dict) else dict(EMPTY_STATE)


def store_for(handle: AccountHandle) -> SessionStore:
    return DbSessionStore(handle) if handle.backend == "db" else FolderSessionStore(handle)


def open_account_context(settings: Settings, account: AccountHandle | None, *, headless: bool) -> Any:
    """The scraper's one way to get a browser context. ``account=None`` = the legacy default profile."""
    if account is None:
        return browser_mod.open_context(settings, headless=headless)
    return store_for(account).open(settings, headless=headless)


__all__ = [
    "EMPTY_STATE",
    "AccountHandle",
    "AccountSpec",
    "DbSessionStore",
    "FolderSessionStore",
    "SessionStore",
    "open_account_context",
    "proxy_options",
    "store_for",
]
