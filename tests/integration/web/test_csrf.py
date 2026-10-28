"""CSRF: a cross-site page must not be able to drive the local app (e.g. start a LinkedIn
run). Unsafe methods are checked against Origin, then Referer; headerless clients pass."""

from __future__ import annotations

from collections.abc import Iterator
from html import unescape

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sales0.config import get_settings
from sales0.db.engine import session_scope
from sales0.db.models import Keyword, ScrapeRun
from sales0.web.app import create_app
from tests.integration.web.conftest import HX, set_session

EVIL = "https://evil.example"
CSRF_TEXT = "came from another website and was blocked"


def _keyword_count() -> int:
    with session_scope() as db:
        return int(db.scalar(select(func.count(Keyword.id))) or 0)


def _run_count() -> int:
    with session_scope() as db:
        return int(db.scalar(select(func.count(ScrapeRun.id))) or 0)


@pytest.fixture
def local_client(db_url: str) -> Iterator[TestClient]:
    """A client whose Host header is a loopback address on a non-default port."""
    with TestClient(create_app(), base_url="http://127.0.0.1:5999") as c:
        yield c


def test_cross_origin_post_is_rejected_with_403_page_and_nothing_written(client: TestClient) -> None:
    resp = client.post("/setup/keywords", data={"text": "ai workshop"}, headers={"Origin": EVIL})
    assert resp.status_code == 403
    assert CSRF_TEXT in unescape(resp.text) and "Traceback" not in resp.text
    assert _keyword_count() == 0

    # The account-safety case: a cross-site form must not start a LinkedIn run.
    set_session("connected")
    client.post("/setup/keywords", data={"text": "ai workshop"})  # headerless: allowed
    started = client.post("/runs", headers={"Origin": EVIL})
    assert started.status_code == 403 and _run_count() == 0


def test_cross_origin_rejection_uses_the_json_envelope_and_htmx_toast(client: TestClient) -> None:
    api = client.post("/runs", headers={"Origin": EVIL, "Accept": "application/json"})
    assert api.status_code == 403
    assert api.json()["ok"] is False and api.json()["error"]["code"] == "csrf_rejected"

    toast = client.post("/setup/keywords", data={"text": "ai workshop"}, headers={**HX, "Origin": EVIL})
    assert toast.headers.get("HX-Retarget") == "#toasts" and "toast--error" in toast.text
    assert CSRF_TEXT in unescape(toast.text) and _keyword_count() == 0


def test_lookalike_and_opaque_origins_are_rejected(client: TestClient) -> None:
    for origin in ("null", "http://127.0.0.1.evil.example:8765", "http://127.0.0.1:9999", "https://127.0.0.1:8765", "garbage"):
        resp = client.post("/setup/keywords", data={"text": "ai workshop"}, headers={"Origin": origin})
        assert resp.status_code == 403, origin
    assert _keyword_count() == 0


def test_same_origin_post_is_allowed_for_configured_and_actual_host(local_client: TestClient) -> None:
    port = get_settings().port  # the harness's pinned test port (never 8765)
    configured = (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
    for i, origin in enumerate((*configured, "http://127.0.0.1:5999", "http://LOCALHOST:5999")):
        resp = local_client.post("/setup/keywords", data={"text": f"ai workshop {i}"}, headers={**HX, "Origin": origin})
        assert resp.status_code == 200 and "toast--error" not in resp.text, (origin, resp.text)
    assert _keyword_count() == 4


def test_rebinding_host_header_is_not_trusted_as_same_origin(db_url: str) -> None:
    with TestClient(create_app(), base_url="http://attacker.example:8765") as c:
        resp = c.post("/setup/keywords", data={"text": "ai workshop"}, headers={"Origin": "http://attacker.example:8765"})
    # refused either by the Host guard (400, DNS rebinding) or by the Origin check (403) - never accepted
    assert resp.status_code in {400, 403} and _keyword_count() == 0


def test_referer_only_cross_origin_is_rejected_and_same_origin_allowed(local_client: TestClient) -> None:
    bad = local_client.post("/setup/keywords", data={"text": "ai workshop"}, headers={"Referer": f"{EVIL}/page?x=1"})
    assert bad.status_code == 403 and _keyword_count() == 0
    good = local_client.post("/setup/keywords", data={"text": "ai workshop"}, headers={**HX, "Referer": "http://127.0.0.1:5999/setup"})
    assert good.status_code == 200 and "<strong>ai workshop</strong>" in good.text and _keyword_count() == 1


def test_origin_takes_precedence_over_a_same_origin_referer(local_client: TestClient) -> None:
    resp = local_client.post(
        "/setup/keywords", data={"text": "ai workshop"}, headers={"Origin": EVIL, "Referer": "http://127.0.0.1:5999/setup"}
    )
    assert resp.status_code == 403 and _keyword_count() == 0


def test_no_origin_or_referer_is_allowed_and_safe_methods_are_never_checked(client: TestClient) -> None:
    resp = client.post("/setup/keywords", data={"text": "ai workshop"}, headers=HX)
    assert resp.status_code == 200 and _keyword_count() == 1
    assert client.get("/setup", headers={"Origin": EVIL}).status_code == 200


def test_rejection_is_logged(client: TestClient) -> None:
    from sales0.web import middleware

    events: list[tuple[str, dict]] = []

    class _Spy:
        def warning(self, event: str, **kw: object) -> None:
            events.append((event, kw))

        def info(self, event: str, **kw: object) -> None:  # the http_request line
            pass

    real = middleware.log
    middleware.log = _Spy()  # type: ignore[assignment]
    try:
        client.post("/runs", headers={"Referer": f"{EVIL}/secret/path?token=abc"})
    finally:
        middleware.log = real
    assert events and events[0][0] == "csrf_rejected"
    fields = events[0][1]
    assert fields["path"] == "/runs" and fields["failed_header"] == "referer"
    assert fields["referer_origin"] == "https://evil.example:443"  # origin only — no path/query logged


def test_live_server_same_origin_and_cross_origin(fixture_server: str) -> None:
    """Real uvicorn on a random port: the browser's own Origin passes, a foreign one is blocked."""
    with httpx.Client(base_url=fixture_server, timeout=15) as http:
        same = http.post("/setup/keywords", data={"text": "ai workshop"}, headers={**HX, "Origin": fixture_server})
        assert same.status_code == 200 and "<strong>ai workshop</strong>" in same.text
        cross = http.post("/setup/keywords", data={"text": "ai consulting"}, headers={"Origin": EVIL})
        assert cross.status_code == 403
    assert _keyword_count() == 1
