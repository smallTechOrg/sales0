"""Fast live-server helpers for the e2e journeys.

* ``live_server`` = ``tests.support.live_server.live_server`` + (a) the database copied from ONE
  migrated template per process instead of an ``alembic upgrade`` subprocess per test, (b) a port
  from a per-xdist-worker slice of 8790-8989 (tests.support.ports; never 8765, never the same port on two workers), and
  (c) a deterministic triage oracle (``tests/e2e/_fakellm``) instead of real Haiku unless the test
  asks for ``real_llm=True`` or ``SALES0_E2E_REAL_LLM=1`` is set. The one full-stack smoke journey
  (phase 1) keeps the real model.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tests.support.live_server import LiveServer
from tests.support.ports import free_port as _free_port

_FAKE_DIR = Path(__file__).with_name("_fakellm")
_template_dir: Path | None = None


def free_port(start: int | None = None) -> int:  # ``start`` kept for call-site compatibility; ignored
    """A free loopback port from this xdist worker's own slice of 8790-8989 (tests.support.ports)."""
    return _free_port()


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
    env.setdefault("SALES0_SCRAPE_PACE_SCALE", os.environ.get("SALES0_TEST_PACE_SCALE", "0.2"))  # fixture-mode-only speed knob
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
