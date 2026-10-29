"""E3: the triage output carries a category SUGGESTION - schema, normalisation, prompt text, cache-friendliness."""

from __future__ import annotations

from sales0.agent.nodes import PROMPT_VERSION, build_prompt
from sales0.agent.schemas import TriageResult, triage_tool_schema
from sales0.agent.state import TriageState
from tests.support.fake_llm import FakeLLMClient, triage_answer
from tests.unit.agent.conftest import OFFERINGS  # noqa: F401


def test_category_defaults_to_relevant_prospect_and_is_normalised() -> None:
    assert TriageResult.model_validate(triage_answer()).category == "relevant_prospect"
    assert TriageResult.model_validate(triage_answer(category=" Channel_Partner ")).category == "channel_partner"
    assert TriageResult.model_validate(triage_answer(category="competitor")).category == "competitor"


def test_unknown_category_is_rejected_so_the_repair_loop_asks_again() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TriageResult.model_validate(triage_answer(category="vendor"))


def test_tool_schema_requires_the_category_with_an_enum() -> None:
    schema = triage_tool_schema()
    assert "category" in schema["required"]
    assert set(schema["properties"]["category"]["enum"]) == {"relevant_prospect", "channel_partner", "competitor"}


def test_prompt_version_is_bumped_and_the_system_prompt_explains_the_category(p1_input, make_deps) -> None:
    assert PROMPT_VERSION == "triage-v4"
    deps = make_deps(FakeLLMClient([triage_answer()]))
    state = TriageState(p1_input, list(deps.offerings), list(deps.examples), PROMPT_VERSION)
    system = build_prompt(state, deps).request.system
    assert "## Category" in system and "`channel_partner`" in system and "`competitor`" in system and "Example O" in system
    # the category text is static scaffolding: it does not depend on the post, so the system block stays cacheable
    state2 = TriageState(p1_input.model_copy(update={"text": "Something else entirely"}), list(deps.offerings), list(deps.examples), PROMPT_VERSION)
    assert build_prompt(state2, deps).request.system == system
