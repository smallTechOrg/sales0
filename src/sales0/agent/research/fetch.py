"""SSRF-safe page fetcher for the research agent.

Guarantees (strict by default):
* only http/https; every hop (initial URL and each redirect, followed manually, max 3) has
  its host resolved and BLOCKED when any address is private / loopback / link-local /
  multicast / reserved / CGNAT / unspecified (IPv4, IPv6 and IPv4-mapped IPv6);
* robots.txt is honoured per origin for the ``sales0-research`` agent (unreachable or 4xx
  = allow, 5xx = disallow all);
* body capped at ``max_bytes`` (truncated), wall-clock timeout per request, content type
  must be text/html or text/plain.

Test seam: ``allow_private_hosts`` lists exact hostnames (e.g. ``"127.0.0.1"``) that are
exempt from the IP block. It is empty by default and must be set explicitly.

Known limit: the address checked is resolved separately from the one httpx connects to
(DNS-rebinding window); acceptable for a human-triggered, low-volume research tool.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

USER_AGENT = "sales0-research/1.0 (+company research; honours robots.txt)"
ROBOTS_AGENT = "sales0-research"
ALLOWED_CONTENT_TYPES = ("text/html", "text/plain", "application/xhtml+xml")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

Resolver = Callable[[str], Iterable[str]]


class FetchError(Exception):
    """A fetch was refused or failed; ``str(exc)`` is safe to show the model as an observation."""


@dataclass
class FetchResult:
    url: str  # the URL requested
    final_url: str  # after redirects
    status: int
    content_type: str
    body: str
    truncated: bool


def default_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError(f"DNS lookup failed for {host}") from exc
    return sorted({str(info[4][0]) for info in infos})


def is_blocked_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return True  # unparseable -> refuse
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        isinstance(ip, ipaddress.IPv6Address)
        and ip.sixtofour is not None
        and ip.sixtofour.is_private
    ):
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT)
    )


class SafeFetcher:
    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        allow_private_hosts: Iterable[str] = (),
        user_agent: str = USER_AGENT,
        max_bytes: int = 200_000,
        timeout_s: float = 10.0,
        max_redirects: int = 3,
        respect_robots: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._resolver: Resolver = resolver or default_resolver
        self._allow_private = frozenset(h.lower() for h in allow_private_hosts)
        self.user_agent = user_agent
        self.max_bytes = max_bytes
        self.timeout_s = timeout_s
        self.max_redirects = max_redirects
        self.respect_robots = respect_robots
        self._client = httpx.Client(
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": user_agent},
        )
        self._robots: dict[
            str, RobotFileParser | bool
        ] = {}  # origin -> parser | True(allow all) | False(deny all)
        self.requested: list[
            str
        ] = []  # every URL actually sent on the wire (incl. robots + hops)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ safety checks
    def check_url(self, url: str) -> tuple[str, str]:
        """Validate scheme + host + resolved addresses. Returns (host, origin) or raises FetchError."""
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise FetchError("invalid URL") from exc
        if parts.scheme not in ("http", "https"):
            raise FetchError(f"scheme '{parts.scheme}' not allowed (http/https only)")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            raise FetchError("URL has no host")
        if parts.username or parts.password:
            raise FetchError("credentials in URL not allowed")
        if host not in self._allow_private:
            addresses = [host] if _is_ip_literal(host) else list(self._resolver(host))
            if not addresses:
                raise FetchError(f"DNS lookup failed for {host}")
            if any(is_blocked_ip(a) for a in addresses):
                raise FetchError(f"blocked: {host} resolves to a non-public address")
        origin = f"{parts.scheme}://{host}" + (f":{port}" if port else "")
        return host, origin

    # ------------------------------------------------------------------ robots
    def _robots_for(
        self, origin: str, deadline: float | None
    ) -> RobotFileParser | bool:
        if origin in self._robots:
            return self._robots[origin]
        verdict: RobotFileParser | bool
        try:
            resp = self._get_following(
                f"{origin}/robots.txt", deadline, host_ok=None, max_bytes=100_000
            )
        except FetchError:
            verdict = True  # unreachable / blocked robots.txt -> allow
        else:
            if resp.status >= 500:
                verdict = False
            elif resp.status >= 400:
                verdict = True
            else:
                parser = RobotFileParser()
                parser.parse(resp.body.splitlines())
                verdict = parser
        self._robots[origin] = verdict
        return verdict

    def robots_allows(
        self, url: str, origin: str, deadline: float | None = None
    ) -> bool:
        verdict = self._robots_for(origin, deadline)
        if isinstance(verdict, bool):
            return verdict
        return verdict.can_fetch(ROBOTS_AGENT, url)

    # ------------------------------------------------------------------ fetching
    def fetch(
        self,
        url: str,
        *,
        deadline: float | None = None,
        host_ok: Callable[[str], bool] | None = None,
    ) -> FetchResult:
        """GET ``url`` (robots-checked at every hop). ``host_ok`` vets each hop's URL (same-site policy)."""
        return self._get_following(
            url, deadline, host_ok=host_ok, max_bytes=self.max_bytes, robots=True
        )

    def _get_following(
        self,
        url: str,
        deadline: float | None,
        *,
        host_ok: Callable[[str], bool] | None,
        max_bytes: int,
        robots: bool = False,
    ) -> FetchResult:
        current = url
        for hop in range(self.max_redirects + 1):
            if host_ok is not None and not host_ok(current):
                raise FetchError("blocked: URL is outside the company's own site")
            _, origin = self.check_url(current)
            if (
                robots
                and self.respect_robots
                and not self.robots_allows(current, origin, deadline)
            ):
                raise FetchError("disallowed by robots.txt")
            timeout = self.timeout_s
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FetchError("time budget exhausted")
                timeout = min(timeout, remaining)
            self.requested.append(current)
            started = time.monotonic()
            try:
                with self._client.stream("GET", current, timeout=timeout) as resp:
                    if resp.status_code in _REDIRECT_STATUSES:
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchError(
                                f"HTTP {resp.status_code} redirect without Location"
                            )
                        current = urljoin(current, location)
                        continue
                    ctype = (
                        resp.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if (
                        resp.status_code < 400
                        and not robots_exempt(current)
                        and ctype not in ALLOWED_CONTENT_TYPES
                    ):
                        raise FetchError(
                            f"content type '{ctype or 'unknown'}' not supported"
                        )
                    chunks: list[bytes] = []
                    size = 0
                    truncated = False
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        chunks.append(chunk)
                        if size > max_bytes:
                            truncated = True
                            break
                        if time.monotonic() - started > timeout:
                            truncated = True
                            break
                    raw = b"".join(chunks)[:max_bytes]
                    encoding = resp.charset_encoding or "utf-8"
                    body = raw.decode(encoding, errors="replace")
                    return FetchResult(
                        url, current, resp.status_code, ctype, body, truncated
                    )
            except FetchError:
                raise
            except httpx.TimeoutException as exc:
                raise FetchError("request timed out") from exc
            except httpx.HTTPError as exc:
                raise FetchError(f"request failed ({type(exc).__name__})") from exc
        raise FetchError(f"too many redirects (max {self.max_redirects})")


def robots_exempt(url: str) -> bool:
    """robots.txt itself may be served as any text type."""
    return urlsplit(url).path == "/robots.txt"


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False
