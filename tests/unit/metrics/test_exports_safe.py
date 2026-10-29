from __future__ import annotations

import pytest

from sales0.services.metrics.exports import _safe


@pytest.mark.parametrize(
    "v",
    ["=1+1", "+1", "@x", "-cmd", "\t=1", "\r=1", "\tfoo", "\rfoo", " =1", "  @x", "\t -cmd", "\n=1"],
)
def test_dangerous_prefixes_are_neutralised(v: str) -> None:
    assert _safe(v).startswith("'")


@pytest.mark.parametrize("v", ["hello", "-5", "-1.5", " 12", "", None, 7, "a=b"])
def test_benign_values_untouched(v: object) -> None:
    assert _safe(v) == ("" if v is None else str(v))
