"""READ-ONLY fetching of public Google Sheets CSV exports.

The only network primitive is :class:`ReadOnlyTransport`, which refuses (raises, before
any I/O) every non-GET request and every host outside the Google Sheets allow-list. The
module contains no credentials, no token flow and no write endpoint, so a sheet write is not
expressible.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Protocol

import httpx

from sales0.services.sheetsync.tabs import TabSpec

ALLOWED_HOSTS = frozenset({"docs.google.com", "sheets.googleapis.com"})
# The CSV export 307-redirects to a per-request download host (GET only, same guard).
_DOWNLOAD_HOST = re.compile(r"^doc-[0-9a-z\-]+-sheets\.googleusercontent\.com$")
EXPORT_URL = "https://docs.google.com/spreadsheets/d/{sheet_id}/export"


class SheetWriteBlocked(RuntimeError):
    """A request that could mutate (or leave the allow-list) was attempted."""


class SheetFetchError(RuntimeError):
    """A tab could not be fetched. ``code`` is a short stable reason; no cell data inside."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def host_allowed(host: str | None) -> bool:
    h = (host or "").lower()
    return h in ALLOWED_HOSTS or bool(_DOWNLOAD_HOST.match(h))


class ReadOnlyTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport | None = None) -> None:
        self._inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method.upper() != "GET":
            raise SheetWriteBlocked(f"{request.method} is not allowed: sheet access is read-only")
        if request.url.scheme != "https" or not host_allowed(request.url.host):
            raise SheetWriteBlocked("request blocked: host/scheme is not on the Google Sheets read allow-list")
        if request.headers.get("content-length", "0") not in ("", "0") or "transfer-encoding" in request.headers:
            raise SheetWriteBlocked("request blocked: a GET with a body is not allowed")
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def make_client(
    *, timeout_s: float = 20.0, inner: httpx.BaseTransport | None = None
) -> httpx.Client:
    return httpx.Client(
        transport=ReadOnlyTransport(inner),
        timeout=httpx.Timeout(timeout_s),
        follow_redirects=True,
        max_redirects=5,
        headers={"User-Agent": "sales0-sheetsync/1 (read-only)"},
    )


class TabSource(Protocol):
    def fetch(self, tab: TabSpec) -> str: ...


class SheetFetcher:
    """Fetch one tab as CSV text with timeout, bounded retries and a size cap."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        retries: int = 3,
        backoff_s: float = 1.0,
        max_bytes: int = 20_000_000,
        timeout_s: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client or make_client(timeout_s=timeout_s)
        self._retries = max(0, retries)
        self._backoff = backoff_s
        self._max_bytes = max_bytes
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def _once(self, url: str, params: dict[str, str]) -> str:
        with self._client.stream("GET", url, params=params) as resp:
            if resp.status_code in (429, 500, 502, 503, 504):
                raise SheetFetchError(f"http-{resp.status_code}")
            if resp.status_code in (401, 403, 404):
                raise SheetFetchError(f"http-{resp.status_code}", "sheet is not publicly readable or does not exist")
            if resp.status_code != 200:
                raise SheetFetchError(f"http-{resp.status_code}")
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self._max_bytes:
                raise SheetFetchError("too-large")
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) > self._max_bytes:
                    raise SheetFetchError("too-large")
            ctype = resp.headers.get("content-type", "").lower()
        text = bytes(buf).decode("utf-8-sig", errors="replace")
        if "text/html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype html", "<html")):
            raise SheetFetchError("not-public", "got an HTML page (login wall?) instead of CSV")
        return text

    def fetch(self, tab: TabSpec) -> str:
        url = EXPORT_URL.format(sheet_id=tab.sheet_id)
        params = {"format": "csv", "gid": tab.gid}
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                return self._once(url, params)
            except SheetFetchError as exc:
                if not (exc.code.startswith("http-") and exc.code[5:] in {"429", "500", "502", "503", "504"}):
                    raise
                last = exc
            except SheetWriteBlocked:
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
            if attempt < self._retries:
                self._sleep(self._backoff * (2**attempt))
        if isinstance(last, SheetFetchError):
            raise last
        raise SheetFetchError("network", type(last).__name__) from last


class StaticSource:
    """Test/fixture source: ``{tab name: csv text}``."""

    def __init__(self, texts: dict[str, str | Exception]) -> None:
        self.texts = texts

    def fetch(self, tab: TabSpec) -> str:
        value = self.texts[tab.name]
        if isinstance(value, Exception):
            raise value
        return value
