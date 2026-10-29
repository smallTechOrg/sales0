"""CRM feedback slice (q12): company matching keys, their frozen migration copies, AI characteristic hints and
the "Add person to track" URL validation."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from sales0.errors import DomainError
from sales0.services.crm import _keys
from sales0.services.crm.characteristics import suggested_keys
from sales0.services.tracked_profiles import parse_profile_url

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "versions" / "q12_traits_9d2c4a71.py"


def _migration():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("q12_frozen", MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WEBSITES = [
    "https://www.Acme.com/about", "acme.com", "www.acme.com/", "http://acme.co.in?utm_source=x", "  https://sub.acme.com  ",
    "https://www.linkedin.com/company/acme", "gmail.com", "not a site", "", None, "a@b.com", "https://bit.ly/x", "localhost",
]
LINKEDINS = [
    "https://www.linkedin.com/company/Acme-Ltd/about/", "linkedin.com/company/acme-ltd", "https://in.linkedin.com/company/acme%20x",
    "https://www.linkedin.com/in/pat/", "https://example.com/company/acme", "", None, "https://www.linkedin.com/school/iit/",
]


@pytest.mark.parametrize(
    ("website", "expected"),
    [("https://www.Acme.com/about", "acme.com"), ("acme.com", "acme.com"), ("www.acme.com/", "acme.com"),
     ("http://acme.co.in?utm_source=x", "acme.co.in"), ("https://www.linkedin.com/company/acme", None), ("gmail.com", None),
     ("not a site", None), ("", None), (None, None), ("a@b.com", None), ("https://bit.ly/x", None)],
)
def test_domain_key(website: str | None, expected: str | None) -> None:
    assert _keys.domain_key(website) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [("https://www.linkedin.com/company/Acme-Ltd/about/", "company/acme-ltd"), ("linkedin.com/company/acme-ltd", "company/acme-ltd"),
     ("https://www.linkedin.com/in/pat/", None), ("https://example.com/company/acme", None), ("", None), (None, None),
     ("https://www.linkedin.com/school/iit/", None)],
)
def test_company_linkedin_key(url: str | None, expected: str | None) -> None:
    assert _keys.company_linkedin_key(url) == expected


def test_frozen_migration_copies_stay_equal() -> None:
    mod = _migration()
    assert mod.GENERIC_HOSTS == _keys.GENERIC_HOSTS
    for w in WEBSITES:
        assert mod.domain_key(w) == _keys.domain_key(w), w
    for u in LINKEDINS:
        assert mod.company_linkedin_key(u) == _keys.company_linkedin_key(u), u


def test_ai_hints_only_suggest_training_provider_for_agencies() -> None:
    assert suggested_keys("training_agency", None) == {"training_provider"}
    assert suggested_keys(None, "agency_requirement") == {"training_provider"}
    assert suggested_keys("buyer_company", "direct_buyer") == set()
    assert suggested_keys(None, None) == set()


@pytest.mark.parametrize(
    ("url", "slug"),
    [("https://www.linkedin.com/in/Pat-Example-123/?trk=x", "in/pat-example-123"), ("linkedin.com/in/asha", "in/asha"),
     ("https://in.linkedin.com/in/r%C3%A1j/", "in/ráj")],
)
def test_parse_profile_url_accepts_person_profiles(url: str, slug: str) -> None:
    assert parse_profile_url(url) == slug


@pytest.mark.parametrize(
    ("url", "needle"),
    [("https://www.linkedin.com/company/acme/", "company page"), ("https://www.linkedin.com/search/results/people/?keywords=x", "search page"),
     ("https://www.linkedin.com/feed/update/urn:li:activity:1/", "post or feed"), ("https://www.linkedin.com/posts/x-activity-1-a", "post link"),
     ("https://lnkd.in/abc", "lnkd.in"), ("https://example.com/in/pat", "not a LinkedIn link"), ("", "Paste"),
     ("https://www.linkedin.com/school/iit/", "school page"), ("https://www.linkedin.com/pub/pat/1/2/3", "old-style")],
)
def test_parse_profile_url_explains_what_was_pasted(url: str, needle: str) -> None:
    with pytest.raises(DomainError) as err:
        parse_profile_url(url)
    assert needle in err.value.message and err.value.code == "profile_url_invalid"
