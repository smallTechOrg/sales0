"""Collision-free loopback ports for parallel (pytest-xdist) runs.

Each xdist worker owns a disjoint slice of 8790-8999 (25 ports per worker, 8 slices), so two workers can
never race for the same port between "bind to probe" and "server starts". Never 8765 (the user's sales0):
the range does not contain it. Without xdist the worker index is 0.
"""

from __future__ import annotations

import os
import socket

RANGE_START = 8790
SLICE = 25
SLICES = 8  # 8790 .. 8989


def worker_index() -> int:
    """0 for a plain run, N for xdist worker ``gwN``."""
    wid = os.environ.get("PYTEST_XDIST_WORKER", "")
    return int(wid[2:]) if wid.startswith("gw") and wid[2:].isdigit() else 0


def under_xdist() -> bool:
    return bool(os.environ.get("PYTEST_XDIST_WORKER"))


def free_port() -> int:
    """First bindable loopback port in this worker's slice."""
    base = RANGE_START + (worker_index() % SLICES) * SLICE
    for port in range(base, base + SLICE):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port in {base}-{base + SLICE - 1}")


def os_free_port() -> int:
    """An OS-assigned ephemeral port (always unique per bind; fine for in-process servers)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
