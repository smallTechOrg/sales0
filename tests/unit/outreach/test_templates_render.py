"""Placeholder rendering/validation (pure) and the draft post-checks."""

from __future__ import annotations

from sales0.services.outreach import templates
from sales0.services.outreach.drafting import check_draft

VALUES = {"first_name": "Priya", "company": "Acme Logistics", "requirement": "GenAI workshop", "sender_name": "Sai", "offering": None}


def test_render_fills_known_and_flags_unfilled_and_unknown() -> None:
    r = templates.render("Hi {first_name}, {{company}} / {offering} / {nonsense}", VALUES)
    assert r.text == "Hi Priya, Acme Logistics / {offering} / {nonsense}"
    assert r.unfilled == ["offering"] and r.unknown == ["nonsense"] and not r.ok


def test_render_blank_value_counts_as_unfilled() -> None:
    r = templates.render("Hello {first_name}", {**VALUES, "first_name": "  "})
    assert r.text == "Hello {first_name}" and r.unfilled == ["first_name"]


def test_render_is_safe_with_braces_that_are_not_placeholders() -> None:
    r = templates.render("json {\"a\": 1} and {1} and {}", VALUES)
    assert r.ok and r.text == "json {\"a\": 1} and {1} and {}"


def test_find_placeholders_covers_braces_and_bracket_gaps() -> None:
    assert templates.find_placeholders("Dear [Your Name], {first_name} {{company}}") == ["first_name", "company", "[Your Name]"]
    assert templates.find_placeholders("clean text, 2 [of 3] things") == []
    assert templates.find_placeholders("broken {{ oops") == ["{{"]


def test_validate_template_lists_unknown_placeholders_and_empty_fields() -> None:
    assert templates.validate_template("T", "Hi {first_name}", "Body {company}") == []
    problems = " ".join(templates.validate_template("", "S", "  {price} {first_name}"))
    assert "name" in problems and "{price}" in problems
    assert "body is empty" in " ".join(templates.validate_template("T", "S", " "))


def test_check_draft_flags_placeholders_words_prices_and_missing_bits() -> None:
    ok_body = "Hello Priya,\n\nAcme Logistics runs 14 warehouses; we run AI workshops.\n\nBest regards,\nSai"
    hard, soft = check_draft("Workshop for Acme Logistics", ok_body, company="Acme Logistics", hooks=["14 warehouses"], source_text="14 warehouses", sender_name="Sai")
    assert hard == [] and soft == []

    hard, _ = check_draft("S", "Hi {first_name} " + "word " * 5, company="X", hooks=[], source_text="", sender_name="")
    assert any("placeholder" in h.lower() for h in hard)
    hard, _ = check_draft("S", "word " * 201, company="", hooks=[], source_text="", sender_name="")
    assert any("201 words" in h for h in hard)
    hard, _ = check_draft("S", "It costs ₹50,000 per head", company="", hooks=[], source_text="workshop", sender_name="")
    assert any("price" in h for h in hard)
    hard, _ = check_draft("S", "Your quoted ₹50,000 fee", company="", hooks=[], source_text="we were quoted ₹50,000", sender_name="")
    assert hard == []  # the figure is in the inputs
    _, soft = check_draft("S", "Hello,\nthanks", company="Acme", hooks=["cold-chain expansion in Nagpur"], source_text="", sender_name="Sai")
    assert len(soft) == 3
