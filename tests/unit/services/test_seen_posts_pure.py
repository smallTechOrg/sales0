"""Pure helpers of services.seen_posts: fingerprint and snippet."""

from __future__ import annotations

import hashlib

from sales0.services.dedupe import normalize_text
from sales0.services.seen_posts import fingerprint, snippet_of


def test_fingerprint_is_sha256_of_author_slug_newline_normalised_text() -> None:
    expected = hashlib.sha256(f"in/priya\n{normalize_text('Hello  WORLD #tag')}".encode()).hexdigest()
    assert fingerprint("in/priya", "Hello  WORLD #tag") == expected
    assert fingerprint("in/priya", "hello world") == expected


def test_fingerprint_differs_by_author_and_treats_none_as_empty_slug() -> None:
    assert fingerprint("in/a", "same text here") != fingerprint("in/b", "same text here")
    assert fingerprint(None, "x y") == fingerprint("", "x y") == hashlib.sha256(b"\nx y").hexdigest()


def test_snippet_is_one_line_and_at_most_240_chars() -> None:
    assert snippet_of("a\n\n  b\tc") == "a b c"
    assert snippet_of("") == ""
    long = snippet_of("word " * 200)
    assert len(long) == 240 and long.endswith("…") and "\n" not in long
    assert snippet_of("x" * 240) == "x" * 240
