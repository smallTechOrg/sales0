"""Maximum raw capture: the card extras the page script reads are parsed, bounded and carried on ``ScrapedPost``."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from sales0.scraper.parsing import parse_count, post_from_raw, raw_card_of
from sales0.worker import protocol as p
from sales0.worker.runner import post_payload

BASE = "https://www.linkedin.com"
RAW = {
    "strategy": "sdui", "urn": "urn:li:activity:7000000000000000001", "component_key": "ck1", "post_href": None,
    "author_name": "Asha Rao", "author_profile_url": "/in/asha", "author_headline": "Head of L&D at Acme",
    "posted_relative": "2w", "text": "We need a GenAI workshop for 30 engineers", "truncated": True, "card_index": 3,
    "via": {"urn": "attr", "text": "sdui_text"},
    "card": {
        "inner_text": "Asha Rao\nWe need a GenAI workshop", "reactions": "1,234", "comments": "56", "reposts": "1.2K",
        "links": ["https://acme.example/jobs"], "hashtags": ["#genai"], "images": 2, "has_video": False,
        "outer_html": "<div>x</div>", "outer_html_len": 12,
    },
}


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1,234", 1234), ("56", 56), ("1.2K", 1200), ("3M", 3_000_000), ("2k", 2000), (7, 7), ("many", None), (None, None), (True, None)],
)
def test_parse_count(value: object, expected: int | None) -> None:
    assert parse_count(value) == expected


def test_post_from_raw_carries_everything() -> None:
    post = post_from_raw(RAW, BASE, date(2026, 10, 29))
    assert post.truncated is True and post.posted_date is not None and post.link_kind == "post"
    card = post.raw_card
    assert card is not None
    assert (card["reactions"], card["comments"], card["reposts"]) == (1234, 56, 1200)
    assert card["reactions_raw"] == "1,234"
    assert card["inner_text"].startswith("Asha Rao") and card["images"] == 2 and card["links"] == ["https://acme.example/jobs"]
    assert card["outer_html"] == "<div>x</div>"
    assert card["author_headline"] == "Head of L&D at Acme" and card["author_profile_url"] == "/in/asha"
    assert card["via"] == {"urn": "attr", "text": "sdui_text"} and card["strategy"] == "sdui"


def test_an_older_page_script_without_card_still_yields_provenance() -> None:
    raw = {k: v for k, v in RAW.items() if k != "card"}
    card = raw_card_of(raw)
    assert card is not None and "inner_text" not in card and card["strategy"] == "sdui"
    assert raw_card_of({}) is None


def test_card_text_and_html_are_capped() -> None:
    raw = {**RAW, "card": {"inner_text": "x" * 50_000, "outer_html": "y" * 20_001, "outer_html_len": 20_001}}
    card = raw_card_of(raw)
    assert card is not None and len(card["inner_text"]) == 20_000 and "outer_html" not in card and card["outer_html_len"] == 20_001


def test_the_wire_payload_carries_raw_card_and_stays_optional_for_older_workers() -> None:
    post = post_from_raw(RAW, BASE, date(2026, 10, 29))
    assert post_payload(post).raw_card == post.raw_card
    older = p.PostPayload(source_ref="urn:li:activity:1", text="hello")  # no raw_card: an older worker
    assert older.raw_card is None
    with pytest.raises(ValidationError):
        p.PostPayload(source_ref="urn:li:activity:1", text="hello", raw_card={"inner_text": "x" * (p.MAX_RAW_CARD_BYTES + 1)})
