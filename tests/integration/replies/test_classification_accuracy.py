"""Real Haiku on a labelled synthetic set: the full pipeline (headers first, then the LLM) must be accurate.

Run with ``-s`` to see every miss. Header-decided cases must be perfect and make no LLM call.
"""

from __future__ import annotations

from collections import Counter

import pytest
from sqlalchemy.orm import Session

from sales0.llm import LLMBadResponse, get_llm_client
from sales0.services import reply_classify
from sales0.services.reply_classify import Classification
from tests.integration.replies.labelled_replies import CASES, Case

pytestmark = pytest.mark.llm

KIND_TARGET, INTENT_TARGET = 0.90, 0.75


class CountingLLM:
    def __init__(self) -> None:
        self.inner = get_llm_client()
        self.calls = 0

    def structured(self, req):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self.inner.structured(req)

    def ping(self):  # type: ignore[no-untyped-def]
        return self.inner.ping()


def pipeline(db: Session, llm: CountingLLM, case: Case) -> Classification:
    """Exactly what ``services.replies.ingest_thread`` does for one message."""
    msg = case.message()
    verdict = reply_classify.classify_by_headers(msg)
    if verdict is not None:
        return verdict
    try:
        return reply_classify.classify_with_llm(db, llm, msg)  # type: ignore[arg-type]
    except LLMBadResponse:
        return Classification("human", "other", "fallback", "fallback")


def test_dataset_is_big_varied_and_well_formed() -> None:
    assert len(CASES) >= 36 and len({c.id for c in CASES}) == len(CASES)
    assert {c.kind for c in CASES} == {"human", "auto_reply", "bounce"}
    assert {c.intent for c in CASES if c.kind == "human"} == {"interested", "question", "not_now", "unsubscribe", "other"}
    assert sum(c.by == "headers" for c in CASES) >= 8
    assert all(c.intent is None for c in CASES if c.kind != "human")
    assert all(c.from_addr.split("@")[1].endswith((".example", "googlemail.com")) for c in CASES)


def test_full_pipeline_accuracy_on_the_labelled_set(db: Session) -> None: 
    llm = CountingLLM()
    kind_ok = intent_ok = humans = 0
    header_wrong: list[str] = []
    misses: list[str] = []
    confusion: Counter[tuple[str, str]] = Counter()
    for case in CASES:
        got = pipeline(db, llm, case)
        if case.by == "headers":
            assert got.by == "headers", f"{case.id}: expected a header decision, got {got.by}"
            if got.kind != case.kind:
                header_wrong.append(f"{case.id}: {case.kind} -> {got.kind}")
        if got.kind == case.kind:
            kind_ok += 1
        else:
            misses.append(f"KIND   {case.id}: expected {case.kind}, got {got.kind} ({got.reason})")
            confusion[(case.kind, got.kind)] += 1
        if case.kind == "human":
            humans += 1
            if got.kind == "human" and got.intent == case.intent:
                intent_ok += 1
            elif got.kind == "human":
                misses.append(f"INTENT {case.id}: expected {case.intent}, got {got.intent} ({got.reason})")
                confusion[(f"intent:{case.intent}", f"intent:{got.intent}")] += 1

    header_cases = sum(c.by == "headers" for c in CASES)
    kind_acc, intent_acc = kind_ok / len(CASES), intent_ok / humans
    print(f"\nKIND accuracy   {kind_ok}/{len(CASES)} = {kind_acc:.1%} (target {KIND_TARGET:.0%})")
    print(f"INTENT accuracy {intent_ok}/{humans} = {intent_acc:.1%} (target {INTENT_TARGET:.0%}) on human replies")
    print(f"LLM calls: {llm.calls} for {len(CASES)} cases ({header_cases} decided by headers)")
    for line in misses:
        print("  " + line)
    for (want, got), n in confusion.most_common():
        print(f"  confusion {want} -> {got}: {n}")

    assert header_wrong == [], f"header-decided cases must all be right: {header_wrong}"
    assert llm.calls == len(CASES) - header_cases  # headers never reach the model
    assert kind_acc >= KIND_TARGET, f"kind accuracy {kind_acc:.1%}: {misses}"
    assert intent_acc >= INTENT_TARGET, f"intent accuracy {intent_acc:.1%}: {misses}"
