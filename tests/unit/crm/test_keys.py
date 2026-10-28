"""Pure helpers of the CRM, and the proof that the migration's frozen copies equal them."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from sales0.config import REPO_ROOT
from sales0.services.crm import _keys


@pytest.fixture(scope="module")
def mig():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("p6_frozen", Path(REPO_ROOT) / "migrations/versions/p6_crm_5b2e7d14.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NAMES = ["Acme Training", "  acme   TRAINING ", "", None, "Ünïcode  Café", "A\tB\nC"]
PHONES = ["98765 43210", "+91 98765-43210", "(022) 2345 6789", "", None, "abc", "0098765432101234"]
TEXTS = ["Asha <ASHA@Acme.Example>, other@x.io", "no email here", "", None, "first@a.com;second@b.com", "x@y"]


@pytest.mark.parametrize("value", NAMES)
def test_frozen_name_key_equals_the_app_version(mig, value) -> None:  # type: ignore[no-untyped-def]
    assert mig.name_key(value) == _keys.name_key(value)


@pytest.mark.parametrize("value", PHONES)
def test_frozen_phone_digits_equals_the_app_version(mig, value) -> None:  # type: ignore[no-untyped-def]
    assert mig.phone_digits(value) == _keys.phone_digits(value)


@pytest.mark.parametrize("value", TEXTS)
def test_frozen_first_email_equals_the_app_version(mig, value) -> None:  # type: ignore[no-untyped-def]
    assert mig.first_email(value) == _keys.first_email(value)


def test_frozen_search_text_equals_the_app_version(mig) -> None:  # type: ignore[no-untyped-def]
    for args in (("Asha Rao", ["a@x.io"], "Acme"), (None, [], None), ("N" * 800, ["e@x.io"], "C")):
        assert mig.search_text(*args) == _keys.search_text(*args)


def test_name_key_collapses_whitespace_and_lowercases() -> None:
    assert _keys.name_key("  Future   LABS ") == "future labs"
    assert _keys.collapse(" a \n b ") == "a b"


def test_emails_and_phones() -> None:
    assert _keys.first_email("Asha <ASHA@Acme.Example>.") == "asha@acme.example"
    assert _keys.all_emails("a@x.io; B@y.io, a@x.io") == ["a@x.io", "b@y.io"]
    assert _keys.first_email("not an email") is None
    assert _keys.phone_digits("+91 98765 43210") == "9876543210"
    assert _keys.display_phone("98765 43210") == "+919876543210"
    assert _keys.display_phone("12") is None and _keys.display_phone(None) is None


def test_linkedin_slug_forms() -> None:
    for raw in ("https://www.linkedin.com/in/Priya-M/", "linkedin.com/in/priya-m", "in/priya-m", "https://linkedin.com/in/priya-m?x=1"):
        assert _keys.linkedin_slug_of(raw) == "in/priya-m"
    assert _keys.linkedin_slug_of("company/acme") == "company/acme"
    assert _keys.linkedin_slug_of("https://example.com/in/x") is None
    assert _keys.linkedin_slug_of(None) is None and _keys.linkedin_slug_of("  ") is None


def test_website_helpers_match_the_sheet_parsers() -> None:
    from sales0.services.sheetsync import values

    for raw in ("www.Future-Labs.in/x/", "https://evolvv.example", "evolvv.example/courses?utm_source=x", "no website", "a@b.com", None):
        assert _keys.website_key(raw) == values.website_key(raw), raw
    for raw in ("98765 43210", "+1 202 555 0143", "022 2345 6789", "12345"):
        assert _keys.normalise_phone(raw) == values.normalise_phone(raw), raw
    assert _keys.website_host("https://www.future-labs.in/x") == "future-labs.in"


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Priya Menon", "priya menon", True),
        ("Priya Menon", "Priya R Menon", True),
        ("Priya", "Priya Menon", True),
        ("Priya Menon", "Karan Shah", False),
        ("Priya Menon", "Priya Nair", False),
        ("Priya Menon", None, False),
        ("", "", False),
    ],
)
def test_loose_name_matching(a, b, same) -> None:  # type: ignore[no-untyped-def]
    assert _keys.names_match_loosely(a, b) is same


def test_like_escape() -> None:
    assert _keys.like_escape("50%_off\\") == "50\\%\\_off\\\\"
