"""Real Haiku: the same long system prompt sent twice is written to the cache then read.

Observed minimum cacheable prefix on claude-haiku-5-5 (tools + system): ~720-760 tokens
(a 714-token prefix wrote nothing, a ~765-token one cached 560). The real triage system
prompt (~14 KB, ~3.5k tokens) is far above it, so caching engages for triage.
"""

from __future__ import annotations

import uuid

import pytest

from sales0.config import get_settings
from sales0.llm import StructuredRequest, get_llm_client
from sales0.llm.pricing import cost_usd_micros

pytestmark = pytest.mark.llm


def _req(system: str) -> StructuredRequest:
    return StructuredRequest(
        feature="ping",
        system=system,
        messages=[{"role": "user", "content": "Word: hello. Record it."}],
        tool_name="record_word",
        tool_description="Record the word.",
        input_schema={"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]},
        max_tokens=50,
        prompt_version="fixscrape-e-cache-v1",
    )


def test_second_identical_long_system_prompt_reads_from_cache() -> None:
    nonce = uuid.uuid4().hex  # unique prefix so a previous run's cache cannot satisfy call #1
    system = f"Run {nonce}. " + " ".join(f"Rule {i}: classify carefully and call the tool." for i in range(120))
    client = get_llm_client()
    first = client.structured(_req(system))
    second = client.structured(_req(system))
    assert first.tool_input and second.tool_input
    assert first.cache_creation_input_tokens > 0
    assert second.cache_read_input_tokens > 0
    settings = get_settings()
    assert second.cost_usd_micros == cost_usd_micros(
        second.input_tokens, second.output_tokens, settings, second.cache_creation_input_tokens, second.cache_read_input_tokens
    )
    assert second.cost_usd_micros < first.cost_usd_micros


def test_short_system_prompt_still_works_without_caching() -> None:
    resp = get_llm_client().structured(_req("Short system prompt. Call the tool."))
    assert resp.tool_input is not None
    assert resp.cache_read_input_tokens == 0 and resp.cache_creation_input_tokens == 0
