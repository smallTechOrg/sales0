"""Pure guardrails on the parsed triage answer (anti-hallucination, one-line fields, consistency)."""

from __future__ import annotations

from sales0.agent.guards import apply_guards, guard_email, guard_phone, one_line
from sales0.agent.schemas import TriageInput, TriageResult
from tests.support.fake_llm import triage_answer
from tests.unit.agent.conftest import P1_TEXT

IDS = [1, 2, 3, 4]


def _guard(text: str = P1_TEXT, author: str | None = "Priya Menon", **fields) -> TriageResult:
    inp = TriageInput(enquiry_id=1, text=text, author_name=author)
    return apply_guards(TriageResult.model_validate(triage_answer(**fields)), inp, IDS)


def test_invented_email_absent_from_the_text_is_stored_as_null() -> None:
    assert _guard(email="ceo@invented.example").email is None


def test_email_present_in_text_is_kept_and_lower_cased_case_insensitively() -> None:
    assert _guard(email="training@acmelogistics.EXAMPLE").email == "training@acmelogistics.example"


def test_email_with_mailto_prefix_and_trailing_punctuation_is_normalised() -> None:
    assert guard_email("mailto:Training@AcmeLogistics.example.", P1_TEXT) == "training@acmelogistics.example"


ZENTRIX = "Contact: arjun.rao@zentrix.example for the workshop."


def test_truncated_local_part_of_a_longer_email_is_rejected() -> None:
    assert guard_email("rao@zentrix.example", ZENTRIX) is None
    assert guard_email("arjun.rao@zentrix.example", ZENTRIX) == "arjun.rao@zentrix.example"


def test_truncated_domain_or_a_prefix_inside_a_longer_address_is_rejected() -> None:
    assert guard_email("arjun.rao@zentrix.ex", ZENTRIX) is None
    assert guard_email("arjun.rao@zentrix", ZENTRIX) is None
    assert guard_email("rao@x.example", "mail sales+rao@x.example or bob-rao@x.example") is None
    assert guard_email("rao@x.example", "mail rao@x.example-corp.in") is None


def test_email_match_is_bounded_but_case_insensitive_and_allows_trailing_punctuation() -> None:
    text = "Write to Arjun.Rao@Zentrix.Example. Or (lead@quantaworks.example), thanks"
    assert guard_email("arjun.rao@zentrix.example", text) == "arjun.rao@zentrix.example"
    assert guard_email("LEAD@quantaworks.example", text) == "lead@quantaworks.example"
    assert guard_email("x@y.example", "<x@y.example>") == "x@y.example"


def test_email_regex_metacharacters_are_escaped() -> None:
    assert guard_email("a.b@c.example", "write to axb@c.example") is None


def test_email_without_at_sign_is_dropped_even_if_the_words_appear() -> None:
    assert guard_email("Acme Logistics", P1_TEXT) is None


def test_phone_with_digits_in_the_text_is_kept_regardless_of_formatting() -> None:
    assert _guard(phone="+919820011111").phone == "+919820011111"
    assert guard_phone("98200-11111", P1_TEXT) == "98200-11111"


def test_invented_phone_is_dropped() -> None:
    assert _guard(phone="+91 99999 00000").phone is None


def test_phone_shorter_than_eight_digits_is_dropped_even_if_present() -> None:
    assert guard_phone("2001", P1_TEXT) is None


def test_offering_id_outside_the_active_catalogue_becomes_null() -> None:
    assert _guard(offering_id=999).offering_id is None
    assert _guard(offering_id=3).offering_id == 3


def test_discard_with_high_priority_is_stored_low() -> None:
    out = _guard(verdict="discard", priority="high", poster_type="training_agency")
    assert out.verdict == "discard" and out.priority == "low"


def test_genuine_priority_is_left_as_the_model_said() -> None:
    assert _guard(priority="medium").priority == "medium"


def test_reason_is_collapsed_to_one_line_and_truncated_to_160_with_ellipsis() -> None:
    out = _guard(reason="Line one\n\nline   two " + "x" * 300)
    assert "\n" not in out.reason
    assert out.reason.startswith("Line one line two ")
    assert len(out.reason) == 160 and out.reason.endswith("…")


def test_empty_reason_falls_back_to_no_reason_given() -> None:
    assert _guard(reason="  \n ").reason == "No reason given"


def test_empty_summary_is_built_from_company_topic_city() -> None:
    assert _guard(requirement_summary="").requirement_summary == "Acme Logistics - Generative AI workshop - Pune"


def test_empty_summary_without_company_uses_author_then_mode() -> None:
    out = _guard(requirement_summary=" ", company="", topic=None, city=None, mode="online")
    assert out.requirement_summary == "Priya Menon - AI requirement - online"
    out = _guard(author=None, requirement_summary="", company=None, topic=None, city=None, mode="unknown")
    assert out.requirement_summary == "Unknown - AI requirement - unknown"


def test_long_summary_is_one_line_of_at_most_140_chars() -> None:
    out = _guard(requirement_summary="A - " + "b " * 200)
    assert len(out.requirement_summary) <= 140 and "\n" not in out.requirement_summary


def test_blank_text_fields_become_null_and_others_are_stripped() -> None:
    out = _guard(company="  ", contact_person=" Priya ", city="", topic="\t", dates_mentioned=" Nov ")
    assert out.company is None and out.city is None and out.topic is None
    assert out.contact_person == "Priya" and out.dates_mentioned == "Nov"


def test_language_is_lower_cased_capped_and_defaults_to_und() -> None:
    assert _guard(language="EN").language == "en"
    assert _guard(language="  ").language == "und"
    assert len(_guard(language="english-language").language) == 8


def test_one_line_keeps_short_text_unchanged() -> None:
    assert one_line(" short  text ", 160) == "short text"
