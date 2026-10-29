from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from sales0.agent.schemas import DecisionExample, OfferingRef, TriageDeps, TriageInput
from sales0.config import get_settings
from tests.support.fake_llm import FakeLLMClient

P1_TEXT = (
    "Acme Logistics is looking for a training partner for a 2-day Generative AI workshop for 40 managers "
    "at our Pune office (offline), week of 17 Nov. Send proposals to Training@AcmeLogistics.example "
    "or call +91 98200 11111."
)

OFFERINGS = [
    OfferingRef(id=1, name="AI corporate workshops", group="Training", description="Workshops for teams."),
    OfferingRef(id=2, name="Technical AI training", group="Training", description="Hands-on for engineers."),
    OfferingRef(id=3, name="AI consulting & build services", group="Services", description="Build AI solutions."),
    OfferingRef(id=4, name="Products", group="Products", description="smallTech's products."),
]

BASE_TIME = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def example(
    enquiry_id: int,
    decision: str,
    *,
    override: bool = False,
    minutes: int = 0,
    text: str | None = None,
    reason: str | None = None,
) -> DecisionExample:
    if override:
        ai_verdict = "discard" if decision == "accept" else "genuine"
    else:
        ai_verdict = "genuine" if decision == "accept" else "discard"
    return DecisionExample(
        enquiry_id=enquiry_id,
        text=text or f"Post number {enquiry_id} about an AI requirement",
        decision=decision,  # type: ignore[arg-type]
        ai_verdict=ai_verdict,  # type: ignore[arg-type]
        is_override=override,
        reason=reason if reason is not None else ("Agency recruiting trainers" if decision == "discard" else None),
        decided_at=BASE_TIME + timedelta(minutes=minutes),
    )


@pytest.fixture
def p1_input() -> TriageInput:
    return TriageInput(
        enquiry_id=101,
        text=P1_TEXT,
        author_name="Priya Menon",
        author_headline="Head of L&D at Acme Logistics",
        source_url="https://www.linkedin.com/feed/update/urn:li:activity:7300000000000000001/",
        date_posted=date(2026, 10, 26),
    )


@pytest.fixture
def make_deps():
    def _make(fake: FakeLLMClient, *, offerings=None, examples=None) -> TriageDeps:
        return TriageDeps(
            llm=fake,
            offerings=OFFERINGS if offerings is None else offerings,
            examples=examples or [],
            settings=get_settings(),
        )

    return _make
