"""run_triage routing with FakeLLMClient: happy path, repair edge, failures, fatal errors."""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from sales0.agent.graph import run_triage
from sales0.agent.nodes import (
    INVALID_TWICE_MESSAGE,
    PROMPT_VERSION,
    REJECTED_MESSAGE,
    UNAVAILABLE_MESSAGE,
)
from sales0.llm.errors import (
    LLMAuthError,
    LLMBadResponse,
    LLMModelNotFound,
    LLMTransientError,
)
from tests.support.fake_llm import FakeLLMClient, triage_answer
from tests.unit.agent.conftest import example


def test_valid_answer_gives_done_with_one_response_and_guarded_result(p1_input, make_deps) -> None:
    fake = FakeLLMClient([triage_answer(email="training@acmelogistics.example", phone="+91 98200 11111")])
    out = run_triage(p1_input, make_deps(fake))

    assert out.status == "done" and out.error is None
    assert len(out.llm_responses) == 1 and fake.calls == 1
    assert out.result is not None
    assert out.result.email == "training@acmelogistics.example"
    assert out.result.phone == "+91 98200 11111"
    assert out.prompt_version == PROMPT_VERSION == "triage-v1"
    assert out.fewshot_count == 0


def test_hallucinated_contact_details_are_nulled_in_the_outcome(p1_input, make_deps) -> None:
    fake = FakeLLMClient([triage_answer(email="ceo@invented.example", phone="+1 555 0100 999", offering_id=999)])
    out = run_triage(p1_input, make_deps(fake))
    assert out.result is not None
    assert out.result.email is None and out.result.phone is None and out.result.offering_id is None


def test_invalid_first_answer_then_valid_second_gives_done_with_two_responses(p1_input, make_deps) -> None:
    fake = FakeLLMClient([triage_answer(verdict="maybe"), triage_answer()])
    out = run_triage(p1_input, make_deps(fake))

    assert out.status == "done"
    assert len(out.llm_responses) == 2
    repair_req = fake.requests[1]
    assert len(repair_req.messages) == 3
    assistant, tool_result = repair_req.messages[1], repair_req.messages[2]
    assert assistant["role"] == "assistant"
    tool_use = assistant["content"][0]
    assert tool_use["type"] == "tool_use" and tool_use["name"] == "record_triage"
    assert tool_use["input"]["verdict"] == "maybe"
    block = tool_result["content"][0]
    assert block["type"] == "tool_result" and block["tool_use_id"] == tool_use["id"] and block["is_error"] is True
    assert block["content"].startswith("Your record_triage input was invalid: verdict:")
    assert block["content"].endswith("Call record_triage again with corrected fields.")


def test_two_invalid_answers_give_failed_invalid_twice(p1_input, make_deps) -> None:
    fake = FakeLLMClient([triage_answer(priority="urgent"), {"verdict": "genuine"}])
    out = run_triage(p1_input, make_deps(fake))

    assert out.status == "failed"
    assert out.error == INVALID_TWICE_MESSAGE == "AI returned an invalid answer twice"
    assert out.result is None
    assert len(out.llm_responses) == 2 and fake.calls == 2


def test_missing_tool_use_block_is_repaired(p1_input, make_deps) -> None:
    fake = FakeLLMClient([None, triage_answer()])
    out = run_triage(p1_input, make_deps(fake))
    assert out.status == "done" and len(out.llm_responses) == 2
    assert fake.requests[1].messages[1]["content"][0]["input"] == {}


def test_enum_casing_from_the_model_is_tolerated_without_a_repair(p1_input, make_deps) -> None:
    fake = FakeLLMClient([triage_answer(verdict="Genuine", priority=" HIGH", mode="Offline", offering_id="2")])
    out = run_triage(p1_input, make_deps(fake))
    assert out.status == "done" and fake.calls == 1
    assert out.result is not None
    assert (out.result.verdict, out.result.priority, out.result.mode, out.result.offering_id) == (
        "genuine",
        "high",
        "offline",
        2,
    )


def test_transient_failure_gives_failed_llm_unavailable_without_a_second_call(p1_input, make_deps) -> None:
    fake = FakeLLMClient([LLMTransientError("LLM unavailable — retry later (RateLimitError)")])
    out = run_triage(p1_input, make_deps(fake))
    assert out.status == "failed"
    assert out.error == UNAVAILABLE_MESSAGE == "LLM unavailable — retry later"
    assert out.llm_responses == [] and fake.calls == 1


def test_rejected_request_fails_with_a_stable_message_and_logs_the_provider_reason(p1_input, make_deps) -> None:
    provider = "Anthropic rejected the request (HTTP 400, BadRequestError): prompt is too long"
    fake = FakeLLMClient([LLMBadResponse(provider)])
    with capture_logs() as logs:
        out = run_triage(p1_input, make_deps(fake))
    assert out.status == "failed"
    assert out.error == REJECTED_MESSAGE == "AI request rejected — see logs"
    assert "too long" not in out.error and "HTTP 400" not in out.error  # provider detail stays out of the UI
    assert out.llm_responses == [] and fake.calls == 1
    rejected = [e for e in logs if e["event"] == "triage_request_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["provider_message"] == provider
    assert rejected[0]["error_type"] == "bad_response"
    assert rejected[0]["enquiry_id"] == p1_input.enquiry_id
    assert rejected[0]["log_level"] == "warning"


@pytest.mark.parametrize(
    "exc",
    [
        LLMAuthError("Anthropic rejected the key — check SALES0_ANTHROPIC_API_KEY in .env"),
        LLMModelNotFound("Model 'x' not found — check SALES0_LLM_MODEL"),
    ],
)
def test_fatal_llm_errors_propagate(p1_input, make_deps, exc) -> None:
    fake = FakeLLMClient([exc])
    with pytest.raises(type(exc)):
        run_triage(p1_input, make_deps(fake))


def test_fatal_error_on_the_repair_call_also_propagates(p1_input, make_deps) -> None:
    fake = FakeLLMClient([triage_answer(verdict="maybe"), LLMAuthError("Anthropic rejected the key")])
    with pytest.raises(LLMAuthError):
        run_triage(p1_input, make_deps(fake))


def test_fewshot_count_is_the_number_of_examples_given(p1_input, make_deps) -> None:
    examples = [example(1, "accept", minutes=1), example(2, "discard", override=True, minutes=2)]
    fake = FakeLLMClient([triage_answer()])
    out = run_triage(p1_input, make_deps(fake, examples=examples))
    assert out.fewshot_count == 2
    assert fake.requests[0].log_context["fewshot_count"] == 2
