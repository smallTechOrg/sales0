from __future__ import annotations

from sales0.agent.research.extract import extract_html, extract_plain
from sales0.agent.research.site import registrable_site, same_site

PAGE = """<html><head><title> Acme &amp; Co </title><style>.x{}</style><script>var a="SCRIPTY";</script></head>
<body><h1>Hello</h1><p>We make   widgets.</p><noscript>NOSCRIPT</noscript>
<a href="/about#team">About</a> <a href="https://x.org/y">Out</a> <a href="mailto:a@b.c">m</a>
<a href="javascript:void(0)">j</a> <a href="/about">dup</a><script>alert("S2")</script><!-- comment --></body></html>"""


def test_extract_title_text_links():
    p = extract_html(PAGE, "https://www.acme.com/index.html")
    assert p.title == "Acme & Co"
    assert "Hello" in p.text and "We make widgets." in p.text
    assert (
        "SCRIPTY" not in p.text
        and "S2" not in p.text
        and "NOSCRIPT" not in p.text
        and "comment" not in p.text
    )
    assert p.links == ["https://www.acme.com/about", "https://x.org/y"]


def test_extract_survives_garbage():
    p = extract_html("<div><p>unclosed <b>bold <a href='/z'>z", "http://a.com/")
    assert "unclosed" in p.text and p.links == ["http://a.com/z"]


def test_extract_plain():
    assert extract_plain("a  b\n\n\nc").text == "a b\nc"


def test_registrable_site():
    assert registrable_site("www.blog.acme.com") == "acme.com"
    assert registrable_site("acme.co.in") == "acme.co.in"
    assert registrable_site("shop.acme.co.uk") == "acme.co.uk"
    assert registrable_site("127.0.0.1") == "127.0.0.1"
    assert registrable_site("localhost") == "localhost"


def test_same_site():
    assert same_site("https://www.acme.com/a", "http://acme.com/")
    assert same_site("https://blog.acme.com/", "https://www.acme.com/")
    assert not same_site("https://acme.com.evil.org/", "https://acme.com/")
    assert not same_site("https://evilacme.com/", "https://acme.com/")
    assert not same_site("not a url", "https://acme.com/")
