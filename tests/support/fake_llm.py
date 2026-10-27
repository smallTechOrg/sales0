"""Deterministic ``LLMClient`` for unit tests (never used on a real-LLM test path).

Usage::

    fake = FakeLLMClient([triage_answer(verdict="discard"), LLMTransientError("...")])
    fake = FakeLLMClient(responder=lambda req: triage_answer())

Each scripted item is a ``dict`` (returned as the tool input), ``None`` (a response with
no tool_use block), an ``LLMResponse`` (returned as-is) or an ``Exception`` (raised).
Every request is recorded in ``fake.requests``. Costs use the real pricing formula.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from sales0.config import Settings
from sales0.llm.client import LLMResponse, StructuredRequest
from sales0.llm.pricing import cost_usd_micros

FAKE_MODEL = "claude-haiku-5-5"
Scripted = dict[str, Any] | LLMResponse | Exception | None


def make_response(
    tool_input: dict[str, Any] | None,
    *,
    input_tokens: int = 1200,
    output_tokens: int = 150,
    text: str | None = None,
    settings: Settings | None = None,
    model: str = FAKE_MODEL,
    latency_ms: int = 42,
    request_id: str | None = "req_fake",
) -> LLMResponse:
    price_settings = settings or Settings(_env_file=None)  # default prices, no .env read
    return LLMResponse(
        tool_input=tool_input,
        text=text,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd_micros=cost_usd_micros(input_tokens, output_tokens, price_settings),
        latency_ms=latency_ms,
        request_id=request_id,
        stop_reason="tool_use" if tool_input is not None else "max_tokens",
    )


def triage_answer(**overrides: Any) -> dict[str, Any]:
    """A valid ``record_triage`` tool input (a genuine buyer); override any field."""
    base: dict[str, Any] = {
        "verdict": "genuine",
        "reason": "Company seeking a training partner for an on-site GenAI workshop",
        "poster_type": "buyer_company",
        "requirement_summary": "Acme Logistics - GenAI workshop for 40 managers - Pune/Offline",
        "priority": "high",
        "offering_id": 1,
        "company": "Acme Logistics",
        "contact_person": "Priya Menon",
        "email": None,
        "phone": None,
        "city": "Pune",
        "mode": "offline",
        "dates_mentioned": "week of 17 Nov",
        "topic": "Generative AI workshop",
        "language": "en",
    }
    base.update(overrides)
    return base


class FakeLLMClient:
    """Scripted or rule-based stand-in for ``AnthropicClient``."""

    def __init__(
        self,
        script: Iterable[Scripted] | None = None,
        *,
        responder: Callable[[StructuredRequest], Scripted] | None = None,
        settings: Settings | None = None,
        model: str = FAKE_MODEL,
    ) -> None:
        if script is None and responder is None:
            raise ValueError("FakeLLMClient needs a script or a responder")
        self._script = list(script) if script is not None else None
        self._responder = responder
        self._settings = settings
        self.model = model
        self.requests: list[StructuredRequest] = []
        self.pings = 0

    @property
    def calls(self) -> int:
        return len(self.requests)

    def _materialise(self, item: Scripted) -> LLMResponse:
        if isinstance(item, Exception):
            raise item
        if isinstance(item, LLMResponse):
            return item
        return make_response(item, settings=self._settings, model=self.model)

    def structured(self, req: StructuredRequest) -> LLMResponse:
        self.requests.append(req.model_copy(deep=True))
        if self._script is not None:
            if not self._script:
                raise AssertionError(f"FakeLLMClient script exhausted after {len(self.requests) - 1} call(s)")
            return self._materialise(self._script.pop(0))
        assert self._responder is not None
        return self._materialise(self._responder(req))

    def ping(self) -> LLMResponse:
        self.pings += 1
        return make_response(None, input_tokens=8, output_tokens=2, text="OK", settings=self._settings, model=self.model)


__all__ = ["FAKE_MODEL", "FakeLLMClient", "make_response", "triage_answer"]
