"""DNS-rebinding defence: validate once, pin the validated IP, re-check the peer."""

from __future__ import annotations

import httpcore
import pytest

from sales0.agent.research.fetch import FetchError, SafeFetcher
from tests.support.research_site import serve_fixture_site

PUBLIC = "93.184.216.34"


class FakeStream:
    def __init__(self, peer: str) -> None:
        self.peer = peer
        self.closed = False

    def get_extra_info(self, name: str):  # type: ignore[no-untyped-def]
        return (self.peer, 80) if name == "server_addr" else None

    def close(self) -> None:
        self.closed = True


class RecordingBackend(httpcore.NetworkBackend):
    def __init__(self, peer: str | None = None) -> None:
        self.connected: list[str] = []
        self.peer = peer

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):  # type: ignore[no-untyped-def]
        self.connected.append(host)
        if self.peer is not None:
            return FakeStream(self.peer)
        raise httpcore.ConnectError("test: no network")


def rebinding_resolver():  # type: ignore[no-untyped-def]
    answers = [[PUBLIC], ["127.0.0.1"], ["127.0.0.1"], ["127.0.0.1"]]
    calls: list[str] = []

    def resolve(host: str) -> list[str]:
        calls.append(host)
        return answers[min(len(calls) - 1, len(answers) - 1)]

    return resolve, calls


def test_rebinding_connects_to_the_validated_ip_never_the_second_answer() -> None:
    backend = RecordingBackend()
    resolve, calls = rebinding_resolver()
    f = SafeFetcher(resolver=resolve, network_backend=backend, respect_robots=False)
    with pytest.raises(FetchError):
        f.fetch("http://evil.example/")
    assert backend.connected == [PUBLIC]  # pinned, no re-resolution, never 127.0.0.1
    assert calls == ["evil.example"]  # resolved exactly once for the hop


def test_second_resolution_to_private_is_refused_per_hop() -> None:
    backend = RecordingBackend()
    resolve, _ = rebinding_resolver()
    f = SafeFetcher(resolver=resolve, network_backend=backend, respect_robots=False)
    f.fetch  # noqa: B018
    resolve("x")  # consume the public answer so the next hop's validation sees 127.0.0.1
    with pytest.raises(FetchError, match="non-public"):
        f.fetch("http://evil.example/")
    assert backend.connected == []


def test_peer_address_is_rechecked_after_connect() -> None:
    backend = RecordingBackend(peer="127.0.0.1")
    f = SafeFetcher(resolver=lambda h: [PUBLIC], network_backend=backend, respect_robots=False)
    with pytest.raises(FetchError):
        f.fetch("http://evil.example/")
    assert backend.connected == [PUBLIC]


def test_fixture_site_still_fetchable_with_private_allowlist() -> None:
    with serve_fixture_site() as site:
        f = SafeFetcher(allow_private_hosts=["127.0.0.1"])
        try:
            r = f.fetch(site.url("/index.html"))
        finally:
            f.close()
    assert r.status == 200 and "<" in r.body


def test_private_host_without_allowlist_still_blocked_for_fixture_site() -> None:
    with serve_fixture_site() as site:
        f = SafeFetcher()
        try:
            with pytest.raises(FetchError, match="non-public"):
                f.fetch(site.url("/index.html"))
        finally:
            f.close()
