from __future__ import annotations

import httpx
import pytest

from sales0.agent.research.fetch import FetchError, SafeFetcher, is_blocked_ip

PUBLIC = "93.184.216.34"


def make(handler, *, resolver=None, **kw) -> tuple[SafeFetcher, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    f = SafeFetcher(
        resolver=resolver or (lambda h: [PUBLIC]),
        transport=httpx.MockTransport(wrapped),
        **kw,
    )
    return f, seen


def html(
    body: str = "<p>hi</p>",
    status: int = 200,
    ctype: str = "text/html; charset=utf-8",
    **headers,
) -> httpx.Response:
    return httpx.Response(
        status, content=body.encode(), headers={"content-type": ctype, **headers}
    )


def site(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    return html()


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.1", "169.254.169.254", "::1", "fe80::1", "fc00::1", "100.64.0.1",
     "0.0.0.0", "224.0.0.1", "240.0.0.1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "garbage"],
)  # fmt: skip
def test_blocked_ips(ip):
    assert is_blocked_ip(ip)


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_ips_pass(ip):
    assert not is_blocked_ip(ip)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/", "http://127.0.0.1:8765/", "http://10.1.2.3/x", "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/", "http://[::ffff:7f00:1]/", "http://localhost/", "http://0.0.0.0/",
        "file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/", "javascript:alert(1)",
        "http://user:pw@example.com/", "http:///nohost",
    ],
)  # fmt: skip
def test_strict_default_blocks(url):
    # localhost resolves (via the injected resolver) to loopback, like the real system resolver.
    f, seen = make(
        site, resolver=lambda h: ["127.0.0.1"] if h == "localhost" else [PUBLIC]
    )
    with pytest.raises(FetchError):
        f.fetch(url)
    assert seen == []


def test_dns_name_resolving_to_private_is_blocked():
    f, seen = make(site, resolver=lambda h: ["10.0.0.7"])
    with pytest.raises(FetchError, match="non-public"):
        f.fetch("http://internal.example.com/")
    assert seen == []


def test_dns_mixed_public_and_private_is_blocked():
    f, seen = make(site, resolver=lambda h: [PUBLIC, "192.168.0.9"])
    with pytest.raises(FetchError):
        f.fetch("http://rebind.example.com/")
    assert seen == []


def test_dns_failure_is_a_fetch_error():
    def boom(host):
        raise FetchError("DNS lookup failed for x")

    f, _ = make(site, resolver=boom)
    with pytest.raises(FetchError, match="DNS"):
        f.fetch("http://nope.example.com/")


def test_redirect_into_private_space_is_blocked_on_the_hop():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
        )

    f, seen = make(handler)
    with pytest.raises(FetchError, match="non-public"):
        f.fetch("http://www.example.com/start")
    assert all(r.url.host != "169.254.169.254" for r in seen)


def test_redirect_to_dns_name_resolving_private_is_blocked():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(301, headers={"location": "http://sneaky.example.com/"})

    f, seen = make(
        handler,
        resolver=lambda h: ["10.9.9.9"] if h == "sneaky.example.com" else [PUBLIC],
    )
    with pytest.raises(FetchError):
        f.fetch("http://www.example.com/")
    assert all(r.url.host != "sneaky.example.com" for r in seen)


def test_redirects_followed_up_to_three_hops():
    def handler(request):
        p = request.url.path
        if p == "/robots.txt":
            return httpx.Response(404)
        n = int(p.strip("/") or 0)
        if n < 3:
            return httpx.Response(302, headers={"location": f"/{n + 1}"})
        return html("<p>end</p>")

    f, _ = make(handler)
    res = f.fetch("http://www.example.com/0")
    assert res.final_url.endswith("/3") and "end" in res.body


def test_four_redirects_is_too_many():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        n = int(request.url.path.strip("/") or 0)
        return httpx.Response(302, headers={"location": f"/{n + 1}"})

    f, _ = make(handler)
    with pytest.raises(FetchError, match="too many redirects"):
        f.fetch("http://www.example.com/0")


def test_off_site_redirect_blocked_by_host_policy():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "https://other.org/"})

    f, seen = make(handler)
    with pytest.raises(FetchError, match="outside"):
        f.fetch("http://www.example.com/", host_ok=lambda u: "example.com" in u)
    assert all(r.url.host != "other.org" for r in seen)


def test_allow_private_hosts_is_an_explicit_exception():
    f, seen = make(site, allow_private_hosts=["127.0.0.1"])
    assert f.fetch("http://127.0.0.1:9999/").status == 200
    with pytest.raises(FetchError):
        f.fetch("http://10.0.0.1:9999/")  # only the listed host is exempt


# ------------------------------------------------------------------ robots
ROBOTS = "User-agent: *\nDisallow: /private\n"


def test_robots_disallow_is_honoured_and_never_fetched():
    def handler(request):
        if request.url.path == "/robots.txt":
            return html(ROBOTS, ctype="text/plain")
        return html()

    f, seen = make(handler)
    with pytest.raises(FetchError, match="robots"):
        f.fetch("http://www.example.com/private/x")
    assert [r.url.path for r in seen] == ["/robots.txt"]
    assert f.fetch("http://www.example.com/public").status == 200


def test_robots_specific_agent_rule():
    robots = "User-agent: sales0-research\nDisallow: /\n"
    f, _ = make(
        lambda r: (
            html(robots, ctype="text/plain") if r.url.path == "/robots.txt" else html()
        )
    )
    with pytest.raises(FetchError, match="robots"):
        f.fetch("http://www.example.com/anything")


def test_robots_cached_per_origin():
    f, seen = make(
        lambda r: (
            html(ROBOTS, ctype="text/plain") if r.url.path == "/robots.txt" else html()
        )
    )
    f.fetch("http://www.example.com/a")
    f.fetch("http://www.example.com/b")
    assert [r.url.path for r in seen].count("/robots.txt") == 1


@pytest.mark.parametrize(
    ("status", "allowed"), [(404, True), (403, True), (500, False), (503, False)]
)
def test_robots_status_policy(status, allowed):
    f, _ = make(
        lambda r: httpx.Response(status) if r.url.path == "/robots.txt" else html()
    )
    if allowed:
        assert f.fetch("http://www.example.com/").status == 200
    else:
        with pytest.raises(FetchError, match="robots"):
            f.fetch("http://www.example.com/")


def test_robots_unreachable_is_allow():
    def handler(request):
        if request.url.path == "/robots.txt":
            raise httpx.ConnectError("down")
        return html()

    f, _ = make(handler)
    assert f.fetch("http://www.example.com/").status == 200


def test_robots_can_be_disabled():
    f, seen = make(lambda r: html(), respect_robots=False)
    f.fetch("http://www.example.com/private/x")
    assert [r.url.path for r in seen] == ["/private/x"]


# ------------------------------------------------------------------ caps / types
def test_body_truncated_to_max_bytes():
    f, _ = make(
        lambda r: (
            httpx.Response(404) if r.url.path == "/robots.txt" else html("a" * 5000)
        ),
        max_bytes=1000,
    )
    res = f.fetch("http://www.example.com/")
    assert res.truncated and len(res.body) == 1000


@pytest.mark.parametrize(
    "ctype", ["application/pdf", "image/png", "application/json", ""]
)
def test_content_type_must_be_text(ctype):
    f, _ = make(
        lambda r: (
            httpx.Response(404)
            if r.url.path == "/robots.txt"
            else html("x", ctype=ctype)
        )
    )
    with pytest.raises(FetchError, match="content type"):
        f.fetch("http://www.example.com/f")


def test_plain_text_ok_and_custom_user_agent_sent():
    f, seen = make(
        lambda r: (
            httpx.Response(404)
            if r.url.path == "/robots.txt"
            else html("hello", ctype="text/plain")
        )
    )
    assert f.fetch("http://www.example.com/t.txt").body == "hello"
    assert all(r.headers["user-agent"].startswith("sales0-research") for r in seen)


def test_timeout_maps_to_fetch_error():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.ReadTimeout("slow")

    f, _ = make(handler)
    with pytest.raises(FetchError, match="timed out"):
        f.fetch("http://www.example.com/")


def test_expired_deadline_refuses():
    import time

    f, seen = make(site)
    with pytest.raises(FetchError, match="time budget"):
        f.fetch("http://www.example.com/", deadline=time.monotonic() - 1)
    assert seen == []


def test_http_error_status_is_returned_not_raised():
    f, _ = make(
        lambda r: (
            httpx.Response(404)
            if r.url.path == "/robots.txt"
            else html("nope", status=404)
        )
    )
    assert f.fetch("http://www.example.com/missing").status == 404
