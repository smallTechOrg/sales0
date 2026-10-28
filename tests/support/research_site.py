"""Local fixture website (Acme Logistics) on a random free port, with a request log."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "research_site"


class FixtureSite:
    def __init__(self, port: int, hits: list[str]) -> None:
        self.port = port
        self.hits = hits
        self.base_url = f"http://127.0.0.1:{port}"

    def url(self, path: str) -> str:
        return self.base_url + path


class _Handler(SimpleHTTPRequestHandler):
    hits: list[str]

    def do_GET(self) -> None:  # noqa: N802
        self.hits.append(self.path)
        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@contextmanager
def serve_fixture_site() -> Iterator[FixtureSite]:
    hits: list[str] = []
    handler = type("H", (_Handler,), {"hits": hits})
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(handler, directory=str(SITE_DIR))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield FixtureSite(server.server_address[1], hits)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
