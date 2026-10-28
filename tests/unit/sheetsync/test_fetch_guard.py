"""Read-only guarantees: no non-GET request and no foreign host can leave the process."""

from __future__ import annotations

import pathlib
import re

import httpx
import pytest

import sales0.services.sheetsync as pkg
from sales0.services.sheetsync import ReadOnlyTransport, SheetFetcher, SheetFetchError, SheetWriteBlocked, make_client
from tests.unit.sheetsync import fixtures as fx

TAB = fx.TABS[0]


def _recorder(log: list[httpx.Request], body: str = "a,b\n1,2\n", status: int = 200, ctype: str = "text/csv"):
    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        return httpx.Response(status, text=body, headers={"content-type": ctype})

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
def test_every_non_get_method_is_blocked_before_io(method: str) -> None:
    log: list[httpx.Request] = []
    client = make_client(inner=_recorder(log))
    url = "https://sheets.googleapis.com/v4/spreadsheets/abc/values/A1:append"
    with pytest.raises(SheetWriteBlocked):
        client.request(method, url, content=b"x")
    assert log == []  # the inner transport never saw it


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/spreadsheets/d/x/export",
        "http://docs.google.com/spreadsheets/d/x/export",  # not https
        "https://docs.google.com.evil.test/spreadsheets/d/x/export",
        "https://evil-docs.google.com.test/x",
        "https://sheets.googleusercontent.com.evil.test/x",
    ],
)
def test_foreign_hosts_and_plain_http_are_blocked(url: str) -> None:
    log: list[httpx.Request] = []
    with pytest.raises(SheetWriteBlocked):
        make_client(inner=_recorder(log)).get(url)
    assert log == []


def test_allowed_hosts_and_download_redirect_host_pass() -> None:
    log: list[httpx.Request] = []
    client = make_client(inner=_recorder(log))
    for url in (
        "https://docs.google.com/spreadsheets/d/x/export?format=csv&gid=0",
        "https://sheets.googleapis.com/v4/spreadsheets/x",
        "https://doc-0o-9k-sheets.googleusercontent.com/export/abc/def",
    ):
        assert client.get(url).status_code == 200
    assert [r.method for r in log] == ["GET"] * 3


def test_a_redirect_to_a_foreign_host_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "docs.google.com":
            return httpx.Response(307, headers={"location": "https://evil.test/steal"})
        raise AssertionError("must not reach the foreign host")

    fetcher = SheetFetcher(make_client(inner=httpx.MockTransport(handler)), retries=0)
    with pytest.raises(SheetWriteBlocked):
        fetcher.fetch(TAB)


def test_fetcher_only_ever_issues_get_with_export_params() -> None:
    log: list[httpx.Request] = []
    text = SheetFetcher(make_client(inner=_recorder(log)), retries=0).fetch(TAB)
    assert text == "a,b\n1,2\n"
    (req,) = log
    assert req.method == "GET" and req.url.host == "docs.google.com"
    assert req.url.path == f"/spreadsheets/d/{TAB.sheet_id}/export"
    assert dict(req.url.params) == {"format": "csv", "gid": TAB.gid}
    assert not req.content and "authorization" not in req.headers


def test_package_source_contains_no_write_or_auth_primitives() -> None:
    root = pathlib.Path(pkg.__file__).parent
    banned = [
        r"\.post\(", r"\.put\(", r"\.patch\(", r"\.delete\(", r"method\s*=\s*[\"'](?!GET)", r"values:append",
        r"batchUpdate", r"spreadsheets\.values", r"oauth", r"service_account", r"googleapiclient", r"gspread",
        r"auth/spreadsheets", r"Authorization",
    ]
    for path in root.glob("*.py"):
        code = "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))
        for pattern in banned:
            assert not re.search(pattern, code, re.IGNORECASE), (path.name, pattern)
    # and the only HTTP verb used on a client is GET
    assert 'stream("GET"' in (root / "fetch.py").read_text()


@pytest.mark.parametrize(
    ("status", "code"), [(404, "http-404"), (403, "http-403"), (500, "http-500"), (429, "http-429")]
)
def test_http_errors_map_to_stable_codes_with_retries(status: int, code: str) -> None:
    log: list[httpx.Request] = []
    sleeps: list[float] = []
    f = SheetFetcher(make_client(inner=_recorder(log, status=status)), retries=2, sleep=sleeps.append)
    with pytest.raises(SheetFetchError) as ei:
        f.fetch(TAB)
    assert ei.value.code == code
    retried = status in (429, 500)
    assert len(log) == (3 if retried else 1)
    assert sleeps == ([1.0, 2.0] if retried else [])


def test_retry_then_success_and_timeouts() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("t", request=request)
        if calls["n"] == 2:
            return httpx.Response(503)
        return httpx.Response(200, text="h\n1\n", headers={"content-type": "text/csv"})

    f = SheetFetcher(make_client(inner=httpx.MockTransport(handler)), retries=3, sleep=lambda s: None)
    assert f.fetch(TAB) == "h\n1\n" and calls["n"] == 3

    def always_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("t", request=request)

    f2 = SheetFetcher(make_client(inner=httpx.MockTransport(always_timeout)), retries=1, sleep=lambda s: None)
    with pytest.raises(SheetFetchError) as ei:
        f2.fetch(TAB)
    assert ei.value.code == "network"


def test_size_cap_and_html_login_wall() -> None:
    big = _recorder([], body="x" * 5000)
    with pytest.raises(SheetFetchError) as ei:
        SheetFetcher(make_client(inner=big), retries=0, max_bytes=1000).fetch(TAB)
    assert ei.value.code == "too-large"
    html = _recorder([], body="<!DOCTYPE html><html>sign in</html>", ctype="text/html; charset=utf-8")
    with pytest.raises(SheetFetchError) as ei2:
        SheetFetcher(make_client(inner=html), retries=0).fetch(TAB)
    assert ei2.value.code == "not-public"


def test_bom_is_stripped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content="﻿Name,X\n".encode(), headers={"content-type": "text/csv"})

    assert SheetFetcher(make_client(inner=httpx.MockTransport(handler))).fetch(TAB) == "Name,X\n"


def test_transport_is_the_read_only_wrapper() -> None:
    assert isinstance(make_client()._transport, ReadOnlyTransport)
