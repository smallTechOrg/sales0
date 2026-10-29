"""Inbox feedback A3: the triage rules come from the (team-editable) criteria; the rest of the prompt is fixed."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from sales0.agent import run_triage
from sales0.agent.criteria import DEFAULT_EXCLUDE, DEFAULT_INCLUDE, DEFAULT_SET, CriteriaSet, Criterion
from sales0.agent.nodes import PROMPT_VERSION, build_prompt
from sales0.agent.schemas import TriageInput
from sales0.agent.state import TriageState
from tests.support.fake_llm import FakeLLMClient, triage_answer

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "versions" / "q14_criteria_5b8e1f3a.py"


def _system(make_deps, criteria: CriteriaSet | None) -> str:  # type: ignore[no-untyped-def]
    deps = make_deps(FakeLLMClient([triage_answer()]))
    state = TriageState(TriageInput(enquiry_id=1, text="Need AI training"), list(deps.offerings), [], PROMPT_VERSION,
                        criteria=criteria)
    request = build_prompt(state, deps).request
    assert request is not None
    return request.system


def test_the_migration_seed_is_a_frozen_copy_of_the_defaults() -> None:
    spec = importlib.util.spec_from_file_location("q14", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.SEED_INCLUDE == DEFAULT_INCLUDE and module.SEED_EXCLUDE == DEFAULT_EXCLUDE
    assert (len(DEFAULT_INCLUDE), len(DEFAULT_EXCLUDE)) == (5, 8)


def test_defaults_render_the_numbered_rule_lists(make_deps) -> None:  # type: ignore[no-untyped-def]
    system = _system(make_deps, None)
    assert system == _system(make_deps, DEFAULT_SET)
    assert f"`genuine` when ANY of these is true:\n  1. {DEFAULT_INCLUDE[0]}\n" in system
    assert f"  5. {DEFAULT_INCLUDE[4]}\nA named contact route" in system
    assert f"  8. {DEFAULT_EXCLUDE[7]}\n\nHow to tell the cases apart" in system
    assert "{%" not in system and "{{" not in system


def test_team_rules_replace_the_lists_in_order(make_deps) -> None:  # type: ignore[no-untyped-def]
    rules = (
        Criterion("include", "A university asks for a paid faculty development programme on GenAI."),
        Criterion("exclude", "A college asking for free guest lectures."),
        Criterion("exclude", "Hackathon sponsorship requests."),
    )
    system = _system(make_deps, CriteriaSet(rules=rules, version=7))
    assert "  1. A university asks for a paid faculty development programme on GenAI.\nA named contact route" in system
    assert "  1. A college asking for free guest lectures.\n  2. Hackathon sponsorship requests.\n\nHow to tell" in system
    assert DEFAULT_EXCLUDE[0] not in system
    # the fixed scaffolding stays
    assert "## Priority rules" in system and "## Safety" in system and "Example A" in system


def test_empty_lists_say_so(make_deps) -> None:  # type: ignore[no-untyped-def]
    system = _system(make_deps, CriteriaSet(rules=(), version=2))
    assert "(no include rules are active" in system and "(no discard rules are active" in system


def test_outcome_carries_the_criteria_version(make_deps) -> None:  # type: ignore[no-untyped-def]
    deps = make_deps(FakeLLMClient([triage_answer()]))
    import dataclasses

    deps = dataclasses.replace(deps, criteria=CriteriaSet(rules=DEFAULT_SET.rules, version=4))
    out = run_triage(TriageInput(enquiry_id=1, text="Need AI training"), deps)
    assert (out.status, out.criteria_version, out.prompt_version) == ("done", 4, PROMPT_VERSION)
    plain = run_triage(TriageInput(enquiry_id=1, text="Need AI training"), make_deps(FakeLLMClient([triage_answer()])))
    assert plain.criteria_version is None
