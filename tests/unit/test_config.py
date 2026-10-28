"""Phase 1.1 settings: browser channel, safety cool-downs, removed headless switch. Pure."""

from __future__ import annotations

import os
import re

import pytest
from pydantic import ValidationError

from sales0.config import REPO_ROOT, Settings


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("SALES0_"):
            monkeypatch.delenv(key)


def test_phase_1_1_defaults_are_auto_chrome_and_60_15_minute_cool_downs(
    clean_env: None,
) -> None:
    s = Settings(_env_file=None)
    assert s.linkedin_browser_channel == "auto"
    assert (s.safety_cooldown_min, s.safety_cooldown_checkpoint_min) == (60, 15)
    assert s.linkedin_connect_timeout_s == 300


def test_the_headless_setting_no_longer_exists_and_a_stale_env_line_is_tolerated(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALES0_LINKEDIN_HEADLESS", "true")
    s = Settings(_env_file=None)
    assert not hasattr(s, "linkedin_headless")
    assert "linkedin_headless" not in Settings.model_fields


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("chrome", "chrome"),
        ("  Chromium  # tests only", "chromium"),
        ("AUTO", "auto"),
        ("chrome#x", "chrome"),
    ],
)
def test_browser_channel_strips_inline_comments_whitespace_and_case(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv("SALES0_LINKEDIN_BROWSER_CHANNEL", raw)
    assert Settings(_env_file=None).linkedin_browser_channel == expected


@pytest.mark.parametrize("raw", ["firefox", "edge", "chrome-beta"])
def test_an_unknown_browser_channel_fails_loudly(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("SALES0_LINKEDIN_BROWSER_CHANNEL", raw)
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)
    assert "linkedin_browser_channel" in str(caught.value)


def test_an_empty_browser_channel_uses_the_default(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALES0_LINKEDIN_BROWSER_CHANNEL", "")
    assert Settings(_env_file=None).linkedin_browser_channel == "auto"


def test_cool_downs_accept_zero_to_switch_them_off(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "0")
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN", "0")
    s = Settings(_env_file=None)
    assert (s.safety_cooldown_min, s.safety_cooldown_checkpoint_min) == (0, 0)


@pytest.mark.parametrize(
    ("var", "raw"),
    [
        ("SALES0_SAFETY_COOLDOWN_MIN", "-1"),
        ("SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN", "-5"),
        ("SALES0_SAFETY_COOLDOWN_MIN", "ten"),
        ("SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN", "1.5"),
    ],
)
def test_negative_or_non_integer_cool_downs_are_rejected(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, var: str, raw: str
) -> None:
    monkeypatch.setenv(var, raw)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_env_example_lists_the_phase_1_1_settings_in_the_linkedin_block_and_drops_headless() -> (
    None
):
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SALES0_LINKEDIN_HEADLESS" not in text
    names = re.findall(r"^(SALES0_[A-Z0-9_]+)=", text, flags=re.MULTILINE)
    block = names[
        names.index("SALES0_LINKEDIN_MODE") : names.index("SALES0_LINKEDIN_MODE") + 5
    ]
    assert block == [
        "SALES0_LINKEDIN_MODE",
        "SALES0_LINKEDIN_CONNECT_TIMEOUT_S",
        "SALES0_LINKEDIN_BROWSER_CHANNEL",
        "SALES0_SAFETY_COOLDOWN_MIN",
        "SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN",
    ]
    assert "SALES0_LINKEDIN_BROWSER_CHANNEL=auto" in text
    assert (
        "SALES0_SAFETY_COOLDOWN_MIN=60" in text
        and "SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN=15" in text
    )


def test_every_settings_field_is_documented_in_env_example() -> None:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(
        re.findall(r"^#?\s*(SALES0_[A-Z0-9_]+)=", text, flags=re.MULTILINE)
    )
    expected = {
        (
            info.validation_alias
            if isinstance(info.validation_alias, str)
            else f"SALES0_{name.upper()}"
        )
        for name, info in Settings.model_fields.items()
    }
    assert expected <= documented, sorted(expected - documented)


# ----------------------------------------------------------------------- Phase 1.2
def test_permalink_cap_defaults_to_15_and_accepts_zero_to_switch_capture_off(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert Settings(_env_file=None).scrape_permalink_max_per_keyword == 15
    monkeypatch.setenv("SALES0_SCRAPE_PERMALINK_MAX_PER_KEYWORD", "0")
    assert Settings(_env_file=None).scrape_permalink_max_per_keyword == 0
    monkeypatch.setenv("SALES0_SCRAPE_PERMALINK_MAX_PER_KEYWORD", "3")
    assert Settings(_env_file=None).scrape_permalink_max_per_keyword == 3


@pytest.mark.parametrize("raw", ["-1", "many", "2.5"])
def test_a_negative_or_non_integer_permalink_cap_is_rejected(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_PERMALINK_MAX_PER_KEYWORD", raw)
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)
    assert "scrape_permalink_max_per_keyword" in str(caught.value)


def test_env_example_lists_the_permalink_cap_right_after_max_scrolls() -> None:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    names = re.findall(r"^(SALES0_[A-Z0-9_]+)=", text, flags=re.MULTILINE)
    at = names.index("SALES0_SCRAPE_MAX_SCROLLS")
    assert names[at + 1] == "SALES0_SCRAPE_PERMALINK_MAX_PER_KEYWORD"
    assert "SALES0_SCRAPE_PERMALINK_MAX_PER_KEYWORD=15" in text
    comment = text.split("SALES0_SCRAPE_MAX_SCROLLS=", 1)[1].split(
        "SALES0_SCRAPE_PERMALINK_MAX_PER_KEYWORD=", 1
    )[0]
    assert "Copy link to post" in comment and "0" in comment and "clipboard" in comment


def test_llm_price_defaults_are_haiku_5_5_pricing(clean_env: None) -> None:
    s = Settings(_env_file=None)
    assert (s.llm_price_input_usd_per_mtok, s.llm_price_output_usd_per_mtok) == (
        0.10,
        0.50,
    )
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SALES0_LLM_PRICE_INPUT_USD_PER_MTOK=0.10" in text
    assert "SALES0_LLM_PRICE_OUTPUT_USD_PER_MTOK=0.50" in text
    assert (
        "claude-haiku-5-5: $0.10 in / $0.50 out per MTok for prompts up to 100K tokens"
        in text
    )


# ----------------------------------------------------------------------- Phase 1.4
def test_phase_1_4_defaults(clean_env: None) -> None:
    s = Settings(_env_file=None)
    assert (s.scrape_max_keywords_per_run, s.scrape_max_posts_per_keyword) == (40, 80)
    assert (s.scrape_max_posts_per_day, s.scrape_max_runs_per_day) == (2000, 2)  # fixscrape D: conservative default (was 4)
    assert s.scrape_max_scrolls == 20 and s.scrape_no_growth_scrolls == 2
    assert s.scrape_sort == "latest"
    assert s.scrape_date_filters == ("past-week", "past-month")
    assert s.scrape_date_filter == "past-week"
    assert s.scrape_expand_max_per_keyword == 40
    assert s.triage_concurrency == 4 and s.seen_raw_retention_days == 14
    assert s.seed_suggested_keywords is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("past-24h", ("past-24h",)),
        (" Past-Month , past-week ", ("past-month", "past-week")),
        ("past-week,past-month # two passes", ("past-week", "past-month")),
    ],
)
def test_date_filters_parse_from_a_comma_list(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: tuple[str, ...],
) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_DATE_FILTERS", raw)
    s = Settings(_env_file=None)
    assert s.scrape_date_filters == expected
    assert s.scrape_date_filter == expected[0]


@pytest.mark.parametrize("raw", ["past-year", "past-week,yesterday", ",", "  "])
def test_bad_date_filters_are_rejected_but_blank_uses_the_default(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_DATE_FILTERS", raw)
    if not raw.strip():
        assert Settings(_env_file=None).scrape_date_filters == (
            "past-week",
            "past-month",
        )
        return
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)
    assert "scrape_date_filters" in str(caught.value)


def test_the_legacy_single_date_filter_is_a_one_item_list_only_when_filters_is_unset(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALES0_SCRAPE_DATE_FILTER", "past-24h")
    assert Settings(_env_file=None).scrape_date_filters == ("past-24h",)
    monkeypatch.setenv("SALES0_SCRAPE_DATE_FILTERS", "past-month,past-week")
    assert Settings(_env_file=None).scrape_date_filters == ("past-month", "past-week")
    monkeypatch.delenv("SALES0_SCRAPE_DATE_FILTERS")
    monkeypatch.setenv("SALES0_SCRAPE_DATE_FILTER", "forever")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("var", "raw"),
    [
        ("SALES0_TRIAGE_CONCURRENCY", "0"),
        ("SALES0_TRIAGE_CONCURRENCY", "9"),
        ("SALES0_SCRAPE_NO_GROWTH_SCROLLS", "0"),
        ("SALES0_SCRAPE_EXPAND_MAX_PER_KEYWORD", "-1"),
        ("SALES0_SEEN_RAW_RETENTION_DAYS", "-1"),
        ("SALES0_SCRAPE_SORT", "random"),
    ],
)
def test_phase_1_4_ranges_are_enforced(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, var: str, raw: str
) -> None:
    monkeypatch.setenv(var, raw)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_phase_1_4_boundaries_and_sort_case(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALES0_TRIAGE_CONCURRENCY", "8")
    monkeypatch.setenv("SALES0_SEEN_RAW_RETENTION_DAYS", "0")
    monkeypatch.setenv("SALES0_SCRAPE_EXPAND_MAX_PER_KEYWORD", "0")
    monkeypatch.setenv("SALES0_SCRAPE_SORT", " Relevance # default ranking")
    monkeypatch.setenv("SALES0_SEED_SUGGESTED_KEYWORDS", "false")
    s = Settings(_env_file=None)
    assert (
        s.triage_concurrency,
        s.seen_raw_retention_days,
        s.scrape_expand_max_per_keyword,
    ) == (8, 0, 0)
    assert s.scrape_sort == "relevance" and s.seed_suggested_keywords is False


def test_env_example_documents_the_phase_1_4_defaults_and_keeps_the_permalink_cap_after_max_scrolls() -> (
    None
):
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for line in (
        "SALES0_SCRAPE_MAX_KEYWORDS_PER_RUN=40",
        "SALES0_SCRAPE_MAX_POSTS_PER_KEYWORD=80",
        "SALES0_SCRAPE_MAX_POSTS_PER_DAY=2000",
        "SALES0_SCRAPE_MAX_RUNS_PER_DAY=2",
        "SALES0_SCRAPE_MAX_SCROLLS=20",
        "SALES0_SCRAPE_NO_GROWTH_SCROLLS=2",
        "SALES0_SCRAPE_SORT=latest",
        "SALES0_SCRAPE_DATE_FILTERS=past-week,past-month",
        "SALES0_SCRAPE_EXPAND_MAX_PER_KEYWORD=40",
        "SALES0_TRIAGE_CONCURRENCY=4",
        "SALES0_SEEN_RAW_RETENTION_DAYS=14",
        "SALES0_SEED_SUGGESTED_KEYWORDS=true",
    ):
        assert line in text, line
    assert not re.search(r"^SALES0_SCRAPE_DATE_FILTER=", text, flags=re.MULTILINE)
