"""E2E harness: the REAL documented run command as a subprocess (architecture.md → E2E harness).

* A temp dir holds the SQLite file (migrated with ``python -m alembic upgrade head``), the
  data dir (fixture browser profile) and the logs.
* The server is ``[sys.executable, "-m", "sales0"]``. Under ``uv run pytest`` that is the
  project ``.venv`` interpreter, i.e. exactly ``uv run python -m sales0``.
* Env: port 8766 (always passed explicitly; ``tests/conftest.py`` pins the parent's
  ``SALES0_PORT`` to a dead port and every inherited ``SALES0_*`` is dropped),
  ``SALES0_LINKEDIN_MODE=fixture``, temp DB/data dir, JSON logs to ``SALES0_LOG_FILE``,
  fast test pacing, and (Phase 1.1) both safety cool-downs 0 and the bundled Chromium
  channel. ``extra_env`` overrides any of them. The Anthropic key is NOT passed here: the
  child reads the repo-root ``.env`` itself, like the real command does.
* Port 8765 is the user's own sales0: the harness refuses it outright.
* ``restart()`` keeps the DB and data dir (state-survival check). ``stop()`` always kills
  the whole process group (uvicorn + the Playwright driver + Chromium), and an ``atexit``
  hook stops any server a crashed test left behind.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from sales0.config import REPO_ROOT

E2E_PORT = 8766
HEALTH_TIMEOUT_S = 30.0

# architecture.md → Conventions → Tests (pacing) + generous caps so reruns never hit them.
TEST_PACING_ENV = {
    "SALES0_SCRAPE_DELAY_MIN_S": "0.05",
    "SALES0_SCRAPE_DELAY_MAX_S": "0.15",
    "SALES0_SCRAPE_KEYWORD_PAUSE_MIN_S": "0.05",
    "SALES0_SCRAPE_KEYWORD_PAUSE_MAX_S": "0.1",
    "SALES0_SCRAPE_MAX_RUNS_PER_DAY": "50",
    # Phase 4: no background scheduler in e2e servers (it would build the daily digest, with an
    # AI call, at boot); the /schedule page and Run now still work.
    "SALES0_SCHEDULER_ENABLED": "false",
}

# architecture.md → E2E harness step 2 (Phase 1.1): safety stops run back to back, and the
# suite never depends on an installed Google Chrome. ``extra_env`` can override both.
PHASE11_ENV = {
    "SALES0_SAFETY_COOLDOWN_MIN": "0",
    "SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN": "0",
    "SALES0_LINKEDIN_BROWSER_CHANNEL": "chromium",
}

# Phase 1.4: startup seeding is off (a test presses "Add suggested keywords" itself) and a run plans
# at most 6 keywords, so a bulk E2E run scans >= 200 posts without taking an hour of paced browsing.
PHASE14_ENV = {
    "SALES0_SEED_SUGGESTED_KEYWORDS": "false",
    "SALES0_SCRAPE_MAX_KEYWORDS_PER_RUN": "6",
}

FORBIDDEN_PORT = 8765

_RUNNING: set[LiveServer] = set()


class LiveServerError(RuntimeError):
    pass


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class LiveServer:
    def __init__(self, root: Path, *, port: int = E2E_PORT, extra_env: dict[str, str] | None = None) -> None:
        extra = dict(extra_env or {})
        if port == FORBIDDEN_PORT or extra.get("SALES0_PORT", "").strip() == str(FORBIDDEN_PORT):
            raise LiveServerError(f"refusing to start on port {FORBIDDEN_PORT} — that is the user's own sales0")
        self.root = root
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.db_path = root / "e2e.db"
        self.db_url = f"sqlite:///{self.db_path.as_posix()}"
        self.data_dir = root / "data"
        self.log_file = root / "server.jsonl"
        self.extra_env = extra
        self.starts = 0
        self._proc: subprocess.Popen[bytes] | None = None
        self._stdout_files: list[Path] = []
        self._migrated = False

    # ------------------------------------------------------------------ env + lifecycle
    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("SALES0_")}
        env.update(
            {
                "PYTHONUNBUFFERED": "1",
                "SALES0_HOST": "127.0.0.1",
                "SALES0_PORT": str(self.port),
                "SALES0_LINKEDIN_MODE": "fixture",
                "SALES0_DATABASE_URL": self.db_url,
                "SALES0_DATA_DIR": str(self.data_dir),
                "SALES0_LOG_FILE": str(self.log_file),
                "SALES0_LOG_FORMAT": "json",
                "SALES0_LOG_LEVEL": "INFO",
                **TEST_PACING_ENV,
                **PHASE11_ENV,
                **PHASE14_ENV,
                **self.extra_env,
            }
        )
        if env["SALES0_PORT"].strip() == str(FORBIDDEN_PORT):  # belt and braces
            raise LiveServerError(f"refusing to start on port {FORBIDDEN_PORT} — that is the user's own sales0")
        return env

    def migrate(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT,
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise LiveServerError(f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}")
        self._migrated = True

    def start(self) -> LiveServer:
        if self._proc is not None:
            raise LiveServerError("server already running")
        if _port_in_use(self.port):
            raise LiveServerError(f"port {self.port} is already in use — stop whatever is listening there first")
        if not self._migrated:
            self.migrate()
        self.starts += 1
        stdout_path = self.root / f"server-{self.starts}.stdout.log"
        self._stdout_files.append(stdout_path)
        with stdout_path.open("wb") as out:
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "sales0"],
                cwd=REPO_ROOT,
                env=self.env(),
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group → stop() reaps Chromium too
            )
        _RUNNING.add(self)
        try:
            self._wait_healthy()
        except BaseException:
            self.stop()
            raise
        return self

    def _wait_healthy(self) -> None:
        assert self._proc is not None
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        last_error = ""
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise LiveServerError(f"server exited with {self._proc.returncode}:\n{self.stdout_tail()}")
            try:
                resp = httpx.get(f"{self.base_url}/api/health", timeout=2.0)
                if resp.status_code == 200:
                    return
                last_error = f"HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                last_error = repr(exc)
            time.sleep(0.2)
        raise LiveServerError(f"/api/health not 200 within {HEALTH_TIMEOUT_S}s ({last_error}):\n{self.stdout_tail()}")

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        _RUNNING.discard(self)
        if proc is None:
            return
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=10)
        # Reap any straggler in the group (e.g. a Chromium child) even after a clean exit.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + 10
        while _port_in_use(self.port) and time.monotonic() < deadline:
            time.sleep(0.1)

    def restart(self) -> LiveServer:
        """Stop and start again on the same DB + data dir (state-survival check)."""
        self.stop()
        return self.start()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------ helpers
    def get_json(self, path: str) -> dict[str, Any]:
        resp = httpx.get(f"{self.base_url}{path}", timeout=10.0)
        resp.raise_for_status()
        body = resp.json()
        assert body.get("ok") is True, body
        return body["data"]

    def log_text(self) -> str:
        return self.log_file.read_text(encoding="utf-8") if self.log_file.exists() else ""

    def log_events(self) -> list[dict[str, Any]]:
        """Every JSON object line the server wrote to ``SALES0_LOG_FILE``."""
        events: list[dict[str, Any]] = []
        for line in self.log_text().splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def stdout_tail(self, lines: int = 60) -> str:
        chunks = []
        for path in self._stdout_files[-1:]:
            if path.exists():
                chunks.extend(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        return "\n".join(chunks)


@contextmanager
def live_server(root: Path, **kwargs: Any) -> Iterator[LiveServer]:
    """Start a live server; it is torn down on exit even when the test fails."""
    server = LiveServer(root, **kwargs)
    try:
        server.start()
        yield server
    finally:
        server.stop()


@atexit.register
def _stop_leftovers() -> None:
    for server in list(_RUNNING):
        server.stop()


__all__ = ["E2E_PORT", "FORBIDDEN_PORT", "PHASE11_ENV", "PHASE14_ENV", "LiveServer", "LiveServerError", "live_server"]
