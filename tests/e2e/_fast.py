"""Fast live-server helpers for the e2e journeys.

* ``live_server`` = ``tests.support.live_server.live_server`` + (a) the database copied from ONE
  migrated template per process instead of an ``alembic upgrade`` subprocess per test, (b) a port
  from a per-xdist-worker slice of 8900-8999 (never 8765, never the same port on two workers), and
  (c) a deterministic triage oracle (``tests/e2e/_fakellm``) instead of real Haiku unless the test
  asks for ``real_llm=True`` or ``SALES0_E2E_REAL_LLM=1`` is set. The one full-stack smoke journey
  (phase 1) keeps the real model.
"""

from __future__ import annotations

import atexit
import os
import shutil
import socket
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tests.support.live_server import LiveServer

PORT_LOW, PORT_HIGH = 8900, 8999
_FAKE_DIR = Path(__file__).with_name("_fakellm")
_template_dir: Path | None = None


def _worker_index() -> int:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    digits = "".join(ch for ch in worker if ch.isdigit())
    return int(digits) if digits else 0


def free_port(start: int | None = None) -> int:  # ``start`` kept for call-site compatibility; ignored
    """A free loopback port in this worker's slice of 8900-8999 (slices of 12, wrapping)."""
    span = PORT_HIGH - PORT_LOW + 1
    offset = (_worker_index() * 12) % span
    for i in range(span):
        port = PORT_LOW + (offset + i) % span
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port in 8900-8999 for the e2e server")


def _template_db() -> Path:
    """A SQLite file migrated to head, built once per process."""
    global _template_dir
    if _template_dir is None:
        _template_dir = Path(tempfile.mkdtemp(prefix="sales0-e2e-tpl-"))
        atexit.register(shutil.rmtree, _template_dir, True)
        LiveServer(_template_dir, port=free_port()).migrate()
    return _template_dir / "e2e.db"


def real_llm_forced() -> bool:
    return os.environ.get("SALES0_E2E_REAL_LLM") == "1"


@contextmanager
def live_server(
    root: Path, *, port: int | None = None, extra_env: dict[str, str] | None = None, real_llm: bool = False
) -> Iterator[LiveServer]:
    env = dict(extra_env or {})
    if not (real_llm or real_llm_forced()):
        env["SALES0_E2E_FAKE_LLM"] = "1"
        env.setdefault("SALES0_ANTHROPIC_API_KEY", "sk-fake-e2e-not-a-real-key")  # startup needs a key; the oracle never calls out
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_FAKE_DIR), env.get("PYTHONPATH", "")]))
    server = LiveServer(root, port=port or free_port(), extra_env=env)
    root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_template_db(), server.db_path)
    server._migrated = True  # noqa: SLF001 - the copied template is already at head
    try:
        server.start()
        yield server
    finally:
        server.stop()
