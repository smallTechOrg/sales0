"""Ground-truth triage eval over the team's real sheets (network + real Haiku).

Not part of the default suite: run with ``uv run pytest -m eval tests/eval``. The sheet text is
fetched at runtime and never written anywhere; failures print row numbers and reasons only.
"""

from __future__ import annotations

import pytest

from sales0.eval_triage import TARGET_DISCARD_PRECISION, TARGET_RECALL, format_report, run_eval

pytestmark = [pytest.mark.eval, pytest.mark.llm]


def test_recall_first_triage_meets_ground_truth_targets() -> None:
    preds, m = run_eval()
    report = format_report(preds, m)
    print("\n" + report)
    assert m.failed == 0, report
    assert m.active_recall >= TARGET_RECALL, report
    if m.discard_precision_all is not None:
        assert m.discard_precision_all >= TARGET_DISCARD_PRECISION, report
