"""fixscrape #6: a failed triage outcome is retried once inside the batch; cost of both attempts kept."""

from __future__ import annotations

import pytest

from sales0.agent import batch
from sales0.agent.batch import BatchItem, triage_concurrently
from sales0.agent.schemas import TriageInput, TriageOutcome
from tests.support.fake_llm import make_response, triage_answer


def _items(n: int) -> list[BatchItem]:
    return [BatchItem(key=i, inp=TriageInput(enquiry_id=i, subject="seen_post", text=f"post {i}")) for i in range(n)]


def test_failed_outcome_is_retried_once_and_costs_are_merged(monkeypatch: pytest.MonkeyPatch, make_deps) -> None:
    from sales0.agent.schemas import TriageResult

    calls: list[int] = []
    resp = make_response(triage_answer())

    def fake(inp, deps):
        calls.append(inp.enquiry_id)
        if len(calls) == 1:
            return TriageOutcome(status="failed", result=None, error="bad JSON", llm_responses=[resp], fewshot_count=0, prompt_version="v")
        return TriageOutcome(status="done", result=TriageResult.model_validate(triage_answer()), error=None,
                             llm_responses=[resp], fewshot_count=0, prompt_version="v")

    monkeypatch.setattr(batch, "run_triage", fake)
    out = list(triage_concurrently(_items(1), make_deps(None), concurrency=1))
    assert len(calls) == 2 and out[0].outcome is not None and out[0].outcome.status == "done"
    assert len(out[0].outcome.llm_responses) == 2


def test_persistent_failure_is_retried_only_once(monkeypatch: pytest.MonkeyPatch, make_deps) -> None:
    calls: list[int] = []

    def fake(inp, deps):
        calls.append(1)
        return TriageOutcome(status="failed", result=None, error="bad JSON", llm_responses=[], fewshot_count=0, prompt_version="v")

    monkeypatch.setattr(batch, "run_triage", fake)
    out = list(triage_concurrently(_items(2), make_deps(None), concurrency=1))
    assert len(calls) == 4 and all(r.outcome and r.outcome.status == "failed" for r in out)
