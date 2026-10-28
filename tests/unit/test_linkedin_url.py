"""The ONE LinkedIn canonicaliser (audit #5 + #13) and the callers that delegate to it."""

from __future__ import annotations

import pytest

from sales0 import linkedin_url as li
from sales0.scraper.urls import profile_slug as scraper_profile_slug
from sales0.services.sheetsync import values as v


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.linkedin.com/in/Pat-Example-123/?trk=x", ("in", "pat-example-123")),
        ("linkedin.com/in/pat", ("in", "pat")),  # scheme-less
        ("www.linkedin.com/in/pat/", ("in", "pat")),
        ("//uk.linkedin.com/in/pat", ("in", "pat")),  # a country sub-domain
        ("HTTPS://IN.LINKEDIN.COM/in/Pat", ("in", "pat")),
        ("https://www.linkedin.com/in/%E0%A4%85%E0%A4%B0%E0%A5%8D%E0%A4%9C%E0%A5%81%E0%A4%A8-1/", ("in", "अर्जुन-1")),  # percent-decoded
        ("https://www.linkedin.com/in/pri%2Dya", ("in", "pri-ya")),
        ("https://www.linkedin.com/in/Café", ("in", "café")),  # NFC + casefold
        ("https://www.linkedin.com/company/Acme-Corp/about/", ("company", "acme-corp")),
        ("https://www.linkedin.com/school/Some-Uni", ("school", "some-uni")),
        ("https://www.linkedin.com/pub/jane-doe/1/2/3", ("pub", "jane-doe")),
        ("https://lnkd.in/p/abc", None),  # short links are left unresolved
        ("https://evil.example/?u=linkedin.com/in/pat", None),  # host check
        ("https://notlinkedin.com/in/pat", None),
        ("https://linkedin.com.evil.example/in/pat", None),
        ("https://www.linkedin.com/feed/update/urn:li:activity:1/", None),
        ("https://www.linkedin.com/in/", None),
        ("ftp://www.linkedin.com/in/pat", None),
        ("/in/relative/", None),  # site-relative only when asked
        ("", None),
        (None, None),
    ],
)
def test_canonical_linkedin(url: str | None, expected: tuple[str, str] | None) -> None:
    assert li.canonical_linkedin(url) == expected


def test_relative_and_fixture_forms_are_opt_in() -> None:
    assert li.canonical_linkedin("/in/relative/", allow_relative=True) == ("in", "relative")
    fixture = "http://127.0.0.1:8800/__fixtures/linkedin/in/arjun-rao/"
    assert li.canonical_linkedin(fixture) is None
    assert li.canonical_linkedin(fixture, allow_fixture=True) == ("in", "arjun-rao")


def test_slug_profile_slug_and_key_forms() -> None:
    assert li.linkedin_slug("https://www.linkedin.com/company/acme") == "acme"
    assert li.profile_slug("https://www.linkedin.com/company/acme") == "company/acme"
    assert li.profile_slug("https://www.linkedin.com/in/Pat") == "in/pat"
    assert li.profile_slug("https://www.linkedin.com/school/uni") is None and li.profile_slug("https://www.linkedin.com/pub/x/1") is None
    # the sheet key keeps the kind: a person keeps the historical bare slug, anything else is kind-prefixed
    assert li.key_of("https://www.linkedin.com/in/Pat") == "pat"
    assert li.key_of("https://www.linkedin.com/company/acme") == "company/acme"
    assert li.key_of("https://lnkd.in/x") is None


def test_every_caller_agrees() -> None:
    url = "https://www.linkedin.com/in/Priya-Menon%2D1/?x"
    assert v.linkedin_slug(url) == "priya-menon-1" and scraper_profile_slug(url) == "in/priya-menon-1"
    assert v.linkedin_key("https://www.linkedin.com/company/Acme") == "company/acme"
    assert scraper_profile_slug("https://example.com/in/x") is None  # the scraper helper checks the host too
    assert scraper_profile_slug("/in/relative/") == "in/relative"
