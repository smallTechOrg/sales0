"""A tiny fake Google (OAuth authorize/token/userinfo/revoke) on a free loopback port.

``/authorize`` immediately redirects back to the ``redirect_uri`` with ``code`` and the
original ``state`` (as if the user clicked Allow). Every request is recorded in ``calls``.
Used by the e2e tests; the app is pointed at it with SALES0_GOOGLE_*_ENDPOINT.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from sales0.integrations.gmail.auth import SCOPES


class FakeGoogle:
    def __init__(self, email: str) -> None:
        self.email = email
        self.calls: list[tuple[str, str]] = []
        self.port = 0

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def env(self) -> dict[str, str]:
        return {
            "SALES0_GOOGLE_AUTH_ENDPOINT": f"{self.base}/authorize",
            "SALES0_GOOGLE_TOKEN_ENDPOINT": f"{self.base}/token",
            "SALES0_GOOGLE_USERINFO_ENDPOINT": f"{self.base}/userinfo",
            "SALES0_GOOGLE_REVOKE_ENDPOINT": f"{self.base}/revoke",
        }

    def paths(self) -> list[str]:
        return [p for _, p in self.calls]


def _handler(fake: FakeGoogle) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def _json(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            fake.calls.append(("GET", parts.path))
            if parts.path == "/authorize":
                q = {k: v[0] for k, v in parse_qs(parts.query).items()}
                target = f"{q['redirect_uri']}?code=fake-auth-code&state={q['state']}"
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif parts.path == "/userinfo":
                self._json(200, {"email": fake.email})
            else:
                self._json(404, {})

        def do_POST(self) -> None:  # noqa: N802
            fake.calls.append(("POST", urlsplit(self.path).path))
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            if urlsplit(self.path).path == "/token":
                self._json(200, {
                    "access_token": "ya29.fake-access", "expires_in": 3600, "token_type": "Bearer",
                    "refresh_token": "1//fake-refresh-token", "scope": " ".join(SCOPES),
                })  # fmt: skip
            elif urlsplit(self.path).path == "/revoke":
                self._json(200, {})
            else:
                self._json(404, {})

    return Handler


@contextmanager
def serve_fake_google(email: str) -> Iterator[FakeGoogle]:
    fake = FakeGoogle(email)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    fake.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
