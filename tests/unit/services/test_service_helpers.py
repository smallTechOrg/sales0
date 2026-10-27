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
    # Phase 1.1 wording: the user resolves it in sales0's own Chrome window via Connect.
    assert "Connect LinkedIn" in msg and "sales0 Chrome window" in msg
    assert "manually" not in msg
    assert "rate-limited" in safety_message("rate_limited", None)
    assert safety_message("logout", "https://www.linkedin.com/authwall?trk=x").startswith(
        "Logged out of LinkedIn (login page at /authwall)")
    assert "sales0 Chrome window" in safety_message("captcha", "https://www.linkedin.com/checkpoint/challenge/x")


def test_cooldown_clock_is_local_24h_rounded_up_and_says_tomorrow_on_a_later_day() -> None:
    from datetime import UTC, datetime

    from sales0.services.runs import cooldown_clock

    now = datetime(2026, 10, 27, 8, 0, tzinfo=UTC)  # 13:30 Asia/Kolkata
    assert cooldown_clock(datetime(2026, 10, 27, 9, 0, tzinfo=UTC), now, "Asia/Kolkata") == "14:30"
    assert cooldown_clock(datetime(2026, 10, 27, 9, 0, 20, tzinfo=UTC), now, "Asia/Kolkata") == "14:31"
    # 19:00 UTC = 00:30 IST on the 28th -> a later local day.
    assert cooldown_clock(datetime(2026, 10, 27, 19, 0, tzinfo=UTC), now, "Asia/Kolkata") == "00:30 tomorrow"


def test_clip_trims_empties_and_truncates_with_an_ellipsis() -> None:
    assert clip("  ", 10) is None
    assert clip(None, 10) is None
    assert clip("abcdefghijkl", 5) == "abcd…"
    assert clip(" ok ", 5) == "ok"
