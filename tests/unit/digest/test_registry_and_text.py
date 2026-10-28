"""Digest registry ordering, plain-text rendering and the summary number check (pure)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest

from sales0.services import digest as dg


@pytest.fixture
def clean_registry() -> Iterator[None]:
    before = dict(dg._REGISTRY)
    yield
    dg._REGISTRY.clear()
    dg._REGISTRY.update(before)


def test_builtin_sections_are_in_display_order() -> None:
    assert [s.key for s in dg.sections()] == [
        "new_relevant_enquiries", "awaiting_review", "outreach", "scrape_health", "cost_today",
    ]  # fmt: skip


def test_a_registered_section_slots_in_by_order_then_key(clean_registry: None) -> None:
    dg.register_section(dg.DigestSection("followups_due", "Follow-ups due", 35, lambda db, w: dg.SectionResult()))
    dg.register_section(dg.DigestSection("a_tie", "Tie", 35, lambda db, w: dg.SectionResult()))
    keys = [s.key for s in dg.sections()]
    assert keys.index("outreach") < keys.index("a_tie") < keys.index("followups_due") < keys.index("scrape_health")


def test_register_replace_false_rejects_a_duplicate_key(clean_registry: None) -> None:
    with pytest.raises(ValueError, match="already registered"):
        dg.register_section(dg.DigestSection("outreach", "X", 1, lambda db, w: dg.SectionResult()), replace=False)


def _view(**kw: object) -> dg.DigestView:
    base = dict(
        id=1, digest_date=date(2026, 10, 28), trigger="manual", window_start=None, window_end=None,
        sections=[
            dg.SectionView("a", "Alpha", [{"text": "Acme - workshop", "href": "/inbox"}, "plain item"], {}, "none"),
            dg.SectionView("b", "Beta", [], {}, "Nothing in beta."),
        ],
        summary_text="Line one.\nLine two.", summary_cost_usd_micros=0, notes=["AI summary skipped."],
        gmail_draft_id=None, gmail_draft_member_id=None, created_at=datetime(2026, 10, 28, tzinfo=UTC),
    )  # fmt: skip
    base.update(kw)
    return dg.DigestView(**base)  # type: ignore[arg-type]


def test_digest_text_renders_summary_notes_items_and_empty_sections() -> None:
    text = dg.digest_text(_view())
    assert text.startswith("sales0 daily digest - 2026-10-28\n")
    assert "At a glance\n  Line one.\n  Line two." in text
    assert "Note: AI summary skipped." in text
    assert "ALPHA\n  - Acme - workshop\n  - plain item" in text
    assert "BETA\n  Nothing in beta." in text
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_digest_text_without_summary_has_no_glance_block() -> None:
    assert "At a glance" not in dg.digest_text(_view(summary_text=None, notes=[]))


def test_unsupported_numbers_flags_only_figures_absent_from_the_data() -> None:
    data = '{"new": 3, "cost": "$0.0123", "runs": 2}'
    assert dg.unsupported_numbers("3 new enquiries, $0.0123 spent over 2 runs.", data) == []
    assert dg.unsupported_numbers("7 new enquiries and 3 runs.", data) == ["7"]
    assert dg.unsupported_numbers("Quiet day.", data) == []
