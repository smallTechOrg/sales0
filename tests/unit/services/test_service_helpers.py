"""Pure helpers: keyword normalisation, offering input stripping, safety messages, clipping."""

from __future__ import annotations

from sales0.services._common import clip
from sales0.services.keywords import normalize_keyword
from sales0.services.session import safety_message
from sales0.services.views import OfferingInput


def test_normalize_keyword_trims_and_collapses_inner_whitespace_keeping_case() -> None:
    assert normalize_keyword("  Corporate   AI training ") == "Corporate AI training"
    assert normalize_keyword(None) == ""


def test_offering_input_strips_and_tolerates_missing_values() -> None:
    data = OfferingInput(name="  AI audits ", group_name=" Services+ ", description=None)  # type: ignore[arg-type]
    assert (data.name, data.group_name, data.description) == ("AI audits", "Services+", "")


def test_safety_message_names_the_page_path_and_the_next_step() -> None:
    msg = safety_message("checkpoint", "http://127.0.0.1:9/__fixtures/linkedin/checkpoint/challenge/?x=1")
    assert msg.startswith("Checkpoint page at /__fixtures/linkedin/checkpoint/challenge/")
    assert "Connect again" in msg
    assert "rate-limited" in safety_message("rate_limited", None)


def test_clip_trims_empties_and_truncates_with_an_ellipsis() -> None:
    assert clip("  ", 10) is None
    assert clip(None, 10) is None
    assert clip("abcdefghijkl", 5) == "abcd…"
    assert clip(" ok ", 5) == "ok"
