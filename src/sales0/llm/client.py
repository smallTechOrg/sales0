"""The provider-neutral LLM contract used by the agent and services."""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

Feature = Literal["triage", "ping", "research", "drafting", "suggest", "reply", "summary"]
# Anthropic ``output_config.effort``. None = the field is not sent (provider default).
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class StructuredRequest(BaseModel):
    """One forced-tool-use call: the model must answer by calling ``tool_name``."""

    feature: Feature
    system: str
    messages: list[dict[str, Any]]  # Anthropic message format: [{"role": "user", "content": "..."}]
    tool_name: str
    tool_description: str
    input_schema: dict[str, Any]  # JSON Schema of the tool input
    max_tokens: int = Field(gt=0)
    prompt_version: str
    log_context: dict[str, Any] = Field(default_factory=dict)  # enquiry_id, run_id, fewshot_count, input_summary
    effort: Effort | None = None  # sent as output_config={"effort": ...} only when set
    # Anthropic server tools sent next to ``tool_name`` (e.g. {"type": "web_search_20250305", "name": "web_search",
    # "max_uses": 3}). With any, set ``tool_choice="auto"``: a forced tool would skip them.
    server_tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: Literal["tool", "auto"] = "tool"
    # False: send the system prompt uncached. With server tools the API otherwise adds its own cache WRITE after the
    # (large) search results, billed at 1.25x, for a request that is never repeated.
    cache_system: bool = True
    # None = the configured default model (``SALES0_LLM_MODEL``). Set for the few calls that need a stronger one
    # (research synthesis runs on ``SALES0_RESEARCH_MODEL``).
    model: str | None = None


class LLMResponse(BaseModel):
    tool_input: dict[str, Any] | None  # parsed tool_use.input (None if the model returned no tool_use)
    text: str | None  # any text block
    model: str
    input_tokens: int  # uncached input tokens (the API's usage.input_tokens)
    output_tokens: int
    cost_usd_micros: int  # includes cache write/read tokens; no DB column for them (folded into cost)
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    latency_ms: int
    request_id: str | None
    stop_reason: str | None
    # Server-side web search (usage.server_tool_use.web_search_requests; its cost is in cost_usd_micros) and the
    # results it returned: [{"url", "title", "page_age"}] (untrusted third-party data).
    web_search_requests: int = 0
    web_results: list[dict[str, Any]] = Field(default_factory=list)


@runtime_checkable
class LLMClient(Protocol):
    def structured(self, req: StructuredRequest) -> LLMResponse: ...

    def ping(self) -> LLMResponse: ...  # max_tokens=5, "Reply with OK"
