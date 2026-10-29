"""Registrable-site comparison (no public-suffix dependency: a small heuristic)."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

# Second-level public suffixes common for Indian / UK / AU / etc. company sites.
_SECOND_LEVEL = frozenset(
    {
        "co",
        "com",
        "org",
        "net",
        "gov",
        "edu",
        "ac",
        "ltd",
        "plc",
        "nic",
        "res",
        "in",
        "firm",
        "gen",
        "ind",
    }
)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def registrable_site(host: str) -> str:
    """``www.blog.acme.co.uk`` -> ``acme.co.uk``; IP literals and single labels are returned as is."""
    host = host.lower().strip(".")
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    take = 3 if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL else 2
    return ".".join(labels[-take:])


def same_site(url_a: str, url_b: str) -> bool:
    a, b = host_of(url_a), host_of(url_b)
    return bool(a) and bool(b) and registrable_site(a) == registrable_site(b)
