from __future__ import annotations

import random
import string
from datetime import date, timedelta

import pytest

from sales0.services.sheetsync import values as v

TODAY = date(2026, 10, 28)


@pytest.mark.parametrize("raw", ["", " ", "-", "–", "Not provided", "NOT PROVIDED", "n/a", "N/A.", "None", None])
def test_null_tokens(raw: str | None) -> None:
    assert v.nullable(raw) is None


def test_nullable_keeps_real_text() -> None:
    assert v.nullable("  hello ") == "hello"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("09 July", date(2026, 7, 9)),
        ("9 jul", date(2026, 7, 9)),
        ("9th July", date(2026, 7, 9)),
        ("July 9", date(2026, 7, 9)),
        ("09 July 2025", date(2025, 7, 9)),
        ("09/07/2026", date(2026, 7, 9)),  # day-first
        ("09-07-26", date(2026, 7, 9)),
        ("2026-07-09", date(2026, 7, 9)),
        ("2026-07-09T10:00:00", date(2026, 7, 9)),
        ("30 Dec", date(2025, 12, 30)),  # not in the future of 28 Oct 2026
        ("28 October", TODAY),
        ("29 Feb", date(2024, 2, 29)),  # nearest valid past leap day
        ("Sept 3", date(2026, 9, 3)),
    ],
)
def test_parse_date(raw: str, expected: date) -> None:
    assert v.parse_date(raw, ref=TODAY) == expected


@pytest.mark.parametrize("raw", ["", "-", "soon", "31 Feb 2026", "99 July", "Foo 12", "13/13/2026", "yesterday"])
def test_parse_date_rejects(raw: str) -> None:
    assert v.parse_date(raw, ref=TODAY) is None


def test_posted_never_after_added() -> None:
    added = date(2026, 1, 5)
    assert v.parse_date("20 Dec", ref=TODAY, not_after=added) == date(2025, 12, 20)
    assert v.parse_date("03 Jan", ref=TODAY, not_after=added) == date(2026, 1, 3)


def test_parse_date_property_inferred_is_nearest_past() -> None:
    rng = random.Random(7)
    for _ in range(500):
        ref = date(2020, 1, 1) + timedelta(days=rng.randrange(0, 3000))
        d = ref - timedelta(days=rng.randrange(0, 700))
        text = f"{d.day:02d} {d.strftime('%B')}"
        got = v.parse_date(text, ref=ref)
        assert got is not None
        assert got <= ref
        assert (got.month, got.day) == (d.month, d.day)
        assert ref - got < timedelta(days=366 * 5)
        if d.year >= ref.year - 0 or (ref - d).days < 365:
            # nearest past occurrence is within one year (leap days excluded)
            if (d.month, d.day) != (2, 29):
                assert (ref - got).days < 366


def test_parse_date_never_raises_on_noise() -> None:
    rng = random.Random(1)
    alphabet = string.ascii_letters + string.digits + " /-.,:*"
    for _ in range(2000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 14)))
        v.parse_date(text, ref=TODAY)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a@b.com", ("a@b.com",)),
        ("A@B.com, c.d@e.org", ("a@b.com", "c.d@e.org")),
        ("a@b.com / a@b.com", ("a@b.com",)),
        ("mail: x.y+z@sub.dom.in — call after 5", ("x.y+z@sub.dom.in",)),
        ("a@b.com,c@d.com", ("a@b.com", "c@d.com")),
        ("Not provided", ()),
        ("no at sign", ()),
        ("", ()),
    ],
)
def test_parse_emails(raw: str, expected: tuple[str, ...]) -> None:
    assert v.parse_emails(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9876543210", ("+919876543210",)),
        ("98765 43210", ("+919876543210",)),
        ("098765 43210", ("+919876543210",)),
        ("+91 98765 43210", ("+919876543210",)),
        ("919876543210", ("+919876543210",)),
        ("(Mob - Dee) +91 90000 11111 / 80000 22222", ("+919000011111", "+918000022222")),
        ("+1 (415) 555-0100", ("+14155550100",)),
        ("Not provided", ()),
        ("-", ()),
        ("123", ()),
        ("call me", ()),
    ],
)
def test_parse_phones(raw: str, expected: tuple[str, ...]) -> None:
    assert v.parse_phones(raw) == expected


def test_phone_property_any_spacing_of_indian_mobile_normalises_identically() -> None:
    rng = random.Random(3)
    for _ in range(300):
        digits = rng.choice("6789") + "".join(rng.choice(string.digits) for _ in range(9))
        spaced = "".join(c + (rng.choice(["", " ", "-", " "]) if i < 9 else "") for i, c in enumerate(digits))
        for prefix in ("", "+91 ", "91", "0"):
            got = v.parse_phones(prefix + spaced)
            assert got == (f"+91{digits}",), (prefix, spaced)


def test_mojibake_repair() -> None:
    assert v.fix_mojibake("weâ€™re") == "we’re"
    assert v.fix_mojibake("ðŸ˜€") == "😀"
    assert v.fix_mojibake("plain ascii and ✨ real emoji") == "plain ascii and ✨ real emoji"
    assert v.fix_mojibake("Ã© is é?") != ""  # never raises


def test_clean_text_caps_and_normalises() -> None:
    text, truncated = v.clean_text("a\r\nb\x00c d", max_chars=100)
    assert (text, truncated) == ("a\nbc d", False)
    text, truncated = v.clean_text("x" * 50, max_chars=10)
    assert len(text) == 10 and truncated


def test_post_keys() -> None:
    a = v.post_key("https://www.linkedin.com/posts/x-activity-7000000000000000001-AbCd?utm_source=share")
    b = v.post_key("https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000001/")
    assert a == b == "post:7000000000000000001"
    assert v.post_key("Some title | text") is None
    assert v.post_key("https://Example.com/A/?utm_x=1&b=2&a=1") == "url:example.com/A?a=1&b=2"


def test_linkedin_slug_and_website_key() -> None:
    assert v.linkedin_slug("https://www.linkedin.com/in/Pat-Example-123/?trk=x") == "pat-example-123"
    assert v.linkedin_slug("https://lnkd.in/p/abc") is None
    assert v.website_key("https://www.rival.example/") == v.website_key("rival.example/") == "rival.example"
    assert v.website_key("no dots") is None
    assert v.website_key("x@y.com") is None
