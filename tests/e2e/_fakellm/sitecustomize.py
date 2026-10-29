"""Loaded by the e2e server subprocess (via PYTHONPATH) when ``SALES0_E2E_FAKE_LLM=1``.

Installs a deterministic triage oracle as the process-wide LLM client so journeys whose point is
the UI/ops flow (not Haiku's judgement) skip the real API. The full-stack smoke journey leaves the
variable unset and keeps the real model."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

if os.environ.get("SALES0_E2E_FAKE_LLM") == "1":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root, for ``tests.support``
    from sales0.llm import set_llm_client
    from tests.support.fake_llm import FakeLLMClient

    # Fixture corpus posts the real model files as genuine (tests/e2e/test_phase1_journey.py).
    _GENUINE_MARKERS = ("Acme Logistics", "Zentrix Retail", "Orbit Health", "Meridian Insurance", "QuantaWorks", "Northwind Logistics", "Brightfield Pharma")

    def _label(text: str) -> str:
        if "safety award" in text:
            return "discard"
        if any(m in text for m in _GENUINE_MARKERS):
            return "genuine"
        digest = hashlib.sha1(text.encode()).digest()[0]
        return "genuine" if digest % 8 == 0 else "discard"

    from sales0.logging import get_logger  # noqa: E402
    from sales0.llm.pricing import micros_to_usd  # noqa: E402

    _log = get_logger("sales0.llm")

    class _LoggingFake(FakeLLMClient):
        """Emits the adapter's ``llm_call`` log line so journeys can count AI calls."""

        def structured(self, req):  # type: ignore[no-untyped-def]
            resp = super().structured(req)
            _log.info(
                "llm_call", feature=req.feature, model=resp.model, prompt_version=req.prompt_version,
                input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
                cache_creation_input_tokens=0, cache_read_input_tokens=0,
                cost_usd=micros_to_usd(resp.cost_usd_micros), latency_ms=resp.latency_ms, status="ok",
                error_type=None, fewshot_count=req.log_context.get("fewshot_count", 0),
                input_summary=str(req.log_context.get("input_summary", ""))[:80], output_summary=None,
                attempts=1, stop_reason=resp.stop_reason, web_search_requests=0,
            )
            return resp

    _base = FakeLLMClient.with_oracle(_label)
    set_llm_client(_LoggingFake(responder=_base._responder, model=_base.model))  # type: ignore[arg-type]
