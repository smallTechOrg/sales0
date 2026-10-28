"""The app boots on a migrated SQLite file: health, root redirect, inbox + static assets, envelope,
request logging, fixture-route isolation, and the real ``python -m sales0`` process."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from fastapi import Form
from fastapi.testclient import TestClient

from sales0.config import REPO_ROOT, get_settings
from sales0.db.engine import session_scope
from sales0.db.migrate_check import DatabaseNotAtHead, alembic_config, head_revision
from sales0.db.models import AppMeta
from sales0.errors import DomainError
from sales0.web.app import create_app


@pytest.fixture
def client(db_url: str) -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def _log_lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def test_health_reports_the_head_revision_and_config_presence(client: TestClient) -> None:
    head = head_revision()
    assert head  # the chain head is read via head_revision(), never pinned
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {
        "status": "ok",
        "db_revision": head,
        "linkedin_mode": "live",
        "model": get_settings().llm_model,
        "api_key_present": True,
    }
    assert resp.headers["x-request-id"]


def test_baseline_migration_creates_and_seeds_app_meta(db_url: str) -> None:
    with session_scope() as db:
        row = db.get(AppMeta, "schema_note")
        assert row is not None and "0001_baseline" in row.value


def test_root_redirects_to_inbox_which_renders_with_assets(client: TestClient) -> None:
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 303 and root.headers["location"] == "/inbox"
    inbox = client.get("/inbox")
    assert inbox.status_code == 200 and "Review inbox" in inbox.text
    assert "/static/css/app.css" in inbox.text and "/static/vendor/htmx.min.js" in inbox.text
    css = client.get("/static/css/app.css")
    assert css.status_code == 200 and "font-family" in css.text
    js = client.get("/static/vendor/htmx.min.js")
    assert js.status_code == 200 and "htmx" in js.text and len(js.content) > 10_000


def test_unknown_api_path_returns_json_error_envelope(client: TestClient) -> None:
    resp = client.get("/api/does-not-exist")
    assert resp.status_code == 404
    assert resp.json() == {"ok": False, "error": {"code": "not_found", "message": "That item no longer exists — refresh the page."}}


def test_unknown_page_renders_the_error_page(client: TestClient) -> None:
    resp = client.get("/no-such-page", headers={"accept": "text/html"})
    assert resp.status_code == 404 and "text/html" in resp.headers["content-type"]
    assert "Back to sales0" in resp.text


def test_fixture_routes_are_not_mounted_in_live_mode(client: TestClient) -> None:
    assert client.get("/__fixtures/linkedin/feed/").status_code == 404


def test_domain_error_renders_json_toast_and_page_shapes(db_url: str) -> None:
    app = create_app()

    @app.get("/api/_boom")
    def api_boom() -> None:
        raise DomainError("job_busy", "LinkedIn is busy (run in progress) — wait for it to finish.", 409)

    @app.post("/_boom")
    def page_boom() -> None:
        raise DomainError("keyword_invalid", "Keyword must be 2–100 characters.", 400)

    with TestClient(app) as c:
        j = c.get("/api/_boom")
        assert j.status_code == 409 and j.json()["error"]["code"] == "job_busy"
        t = c.post("/_boom", headers={"HX-Request": "true"})
        assert t.status_code == 200
        assert t.headers["hx-retarget"] == "#toasts" and t.headers["hx-reswap"] == "beforeend"
        assert '<div class="toast toast--error" role="alert">Keyword must be 2–100 characters.</div>' == t.text
        p = c.post("/_boom")
        assert p.status_code == 400 and "Keyword must be 2–100 characters." in p.text


def test_unhandled_exception_shows_generic_message_with_request_id(db_url: str) -> None:
    app = create_app()

    @app.get("/api/_crash")
    def crash() -> None:
        raise RuntimeError("secret internals")

    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/_crash", headers={"x-request-id": "req-abc123"})
    assert resp.status_code == 500
    message = resp.json()["error"]["message"]
    assert "request_id=req-abc123" in message and "secret internals" not in message


def test_http_request_log_has_field_names_but_not_values(db_url: str, capfd: pytest.CaptureFixture[str]) -> None:
    app = create_app()

    @app.post("/_form")
    def form_echo(text: str = Form(...), password: str = Form(...)) -> dict:
        return {"len": len(text) + len(password)}

    with TestClient(app) as c:
        resp = c.post("/_form?tab=review", data={"text": "hello", "password": "hunter2-value"})
    assert resp.status_code == 200 and resp.json() == {"len": 18}  # the body survived the middleware
    out = capfd.readouterr().out
    assert "hunter2-value" not in out
    lines = [line for line in _log_lines(out) if line.get("event") == "http_request" and line.get("path") == "/_form"]
    assert lines, out[-2000:]
    line = lines[0]
    assert line["input_summary"] == {"query": ["tab"], "form": ["password", "text"]}
    assert line["status"] == 200 and line["method"] == "POST" and line["request_id"]
    assert {"timestamp", "level", "logger", "latency_ms", "htmx", "output_summary"} <= set(line)


def test_app_refuses_to_start_when_db_is_not_at_head() -> None:
    # No db_url fixture: the temp database exists but was never migrated.
    with pytest.raises(DatabaseNotAtHead, match="uv run alembic upgrade head"):
        with TestClient(create_app()):
            pass


# --------------------------------------------------------------------------- real process
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _server_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SALES0_") or k == "SALES0_ANTHROPIC_API_KEY"}
    env.update(
        SALES0_DATABASE_URL=get_settings().resolved_database_url,
        SALES0_DATA_DIR=str(tmp_path / "data"),
        SALES0_LOG_FORMAT="json",
        **extra,
    )
    return env


def test_documented_run_command_boots_and_serves_health(db_url: str, tmp_path: Path) -> None:
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "sales0"],
        cwd=REPO_ROOT,
        env=_server_env(tmp_path, SALES0_PORT=str(port)),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        body = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1)
                if r.status_code == 200:
                    body = r.json()
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        assert body is not None, f"server did not become healthy (exit={proc.poll()})"
        assert body["data"]["db_revision"] == head_revision() and body["data"]["api_key_present"] is True
        root = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)
        assert root.status_code == 303 and root.headers["location"] == "/inbox"
        inbox = httpx.get(f"http://127.0.0.1:{port}/inbox", timeout=5)
        assert inbox.status_code == 200 and "Review inbox" in inbox.text
        assert httpx.get(f"http://127.0.0.1:{port}/static/css/app.css", timeout=2).status_code == 200
    finally:
        proc.terminate()
        try:
            output, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate()
    events = {line.get("event") for line in _log_lines(output)}
    assert {"server_starting", "app_started", "http_request"} <= events, output[-2000:]
    key = get_settings().anthropic_api_key
    assert key is not None and key.get_secret_value() not in output


def test_run_command_refuses_unmigrated_db_and_public_host(tmp_path: Path) -> None:
    unmigrated = subprocess.run(
        [sys.executable, "-m", "sales0"], cwd=REPO_ROOT, env=_server_env(tmp_path), capture_output=True, text=True, timeout=60
    )
    assert unmigrated.returncode == 1 and "Run: uv run alembic upgrade head" in unmigrated.stderr

    command.upgrade(alembic_config(get_settings().resolved_database_url), "head")
    public = subprocess.run(
        [sys.executable, "-m", "sales0"],
        cwd=REPO_ROOT,
        env=_server_env(tmp_path, SALES0_HOST="0.0.0.0"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert public.returncode == 2 and "SALES0_APP_PASSWORD" in public.stderr
