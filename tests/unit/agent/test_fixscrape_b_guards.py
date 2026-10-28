"""Audit #16: phone digit containment per number, and grounding of company/person/city/topic."""

from __future__ import annotations

from sales0.agent.guards import apply_guards, guard_grounded, guard_phone
from sales0.agent.schemas import TriageInput, TriageResult
from tests.support.fake_llm import triage_answer
from tests.unit.agent.conftest import P1_TEXT


def _guard(text: str = P1_TEXT, author: str | None = "Priya Menon", headline: str | None = None, **fields) -> TriageResult:
    inp = TriageInput(enquiry_id=1, text=text, author_name=author, author_headline=headline)
    return apply_guards(TriageResult.model_validate(triage_answer(**fields)), inp, [1])


def test_phone_assembled_from_digits_of_different_numbers_is_dropped() -> None:
    text = "Budget 12345 and batch 6789, ref 55501. Call 98200 11111 for details."
    # "1234567890" never occurs as one number: it only exists across the whole-text digit string.
    assert guard_phone("1234567890", "Seats 12345 then 67890 later") is None
    assert guard_phone("98200 11111", text) == "98200 11111"


def test_phone_matches_a_single_number_with_country_code_variants() -> None:
    assert guard_phone("+91 98200 11111", "call 098200 11111 now") == "+91 98200 11111"
    assert guard_phone("9820011111", "call +91-98200-11111.") == "9820011111"


def test_phone_digits_embedded_inside_a_longer_digit_run_are_dropped() -> None:
    assert guard_phone("98200 11111", "order id 19820011111999") is None


def test_invented_company_person_city_topic_are_dropped_without_failing_the_result() -> None:
    out = _guard(company="Globex Corp", contact_person="Rahul Verma", city="Chennai", topic="Quantum cryptography")
    assert (out.company, out.contact_person, out.city, out.topic) == (None, None, None, None)
    assert out.verdict == "genuine" and out.offering_id == 1


def test_grounded_fields_survive_case_insensitively_and_with_partial_tokens() -> None:
    out = _guard(company="ACME logistics pvt ltd", contact_person="Priya", city="pune", topic="Generative AI")
    assert out.company == "ACME logistics pvt ltd"
    assert out.contact_person == "Priya" and out.city == "pune" and out.topic == "Generative AI"


def test_person_may_be_grounded_in_the_author_headline() -> None:
    out = _guard(author=None, headline="Head of L&D at Zentrix Retail", company="Zentrix Retail")
    assert out.company == "Zentrix Retail"


def test_guard_grounded_helper_contract() -> None:
    assert guard_grounded(None, "x") is None
    assert guard_grounded("  ", "x") is None
    assert guard_grounded("Pune", "in pune office") == "Pune"
    assert guard_grounded("Mumbai", "in pune office") is None
