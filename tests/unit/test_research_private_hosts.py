"""SALES0_RESEARCH_ALLOW_PRIVATE_HOSTS is a test-only switch: honoured only in fixture mode."""

from __future__ import annotations

import pytest

from sales0.config import Settings


@pytest.mark.parametrize(
    ("mode", "value", "expected"),
    [
        ("live", "1", []),
        ("live", "127.0.0.1", []),
        ("fixture", "", []),
        ("fixture", "1", ["127.0.0.1", "localhost"]),
        ("fixture", "10.0.0.5, Intra.Example", ["10.0.0.5", "intra.example"]),
    ],
)
def test_private_hosts_only_in_fixture_mode(mode: str, value: str, expected: list[str]) -> None:
    s = Settings(_env_file=None, linkedin_mode=mode, research_allow_private_hosts=value)  # type: ignore[arg-type]
    assert s.research_private_hosts == expected
