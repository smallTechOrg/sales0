"""Web search step of prospect research: ONE Haiku request with Anthropic's server-side web search tool.

The model may run up to ``max_uses`` searches (Anthropic runs them; billed per search) and then records what it
found with the ``record_web_findings`` tool (``tool_choice=auto`` - a forced tool would skip the searches). Search
results are third-party, untrusted DATA: the findings are later shown to the research agent inside
``<web_findings>`` delimiters, and only URLs that the search actually returned can be cited as sources.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from sales0.agent.research.loop import FATAL_LLM_ERRORS, PROMPTS_DIR, _env
from sales0.config import get_settings
from sales0.db.models import LlmCall
from sales0.llm import LLMClient, LLMError, get_llm_client
from sales0.llm.client import StructuredRequest
from sales0.logging import get_logger

log = get_logger("sales0.agent.research.web")

PROMPT_VERSION = "research-web-v2"
TOOL_NAME = "record_web_findings"
MAX_FINDINGS = 6


@dataclass
class WebFinding:
    fact: str
    url: str
    title: str = ""
    about: str = "company"  # "company" or a person's name


@dataclass
class WebFindings:
    status: str  # done | empty | failed | off
    findings: list[WebFinding] = field(default_factory=list)
    summary: str = ""
    results: list[dict[str, Any]] = field(default_factory=list)  # every result URL the searches returned
    searches: int = 0
    cost_usd_micros: int = 0
    error: str | None = None

    @property
    def urls(self) -> list[str]:
        return list(dict.fromkeys([f.url for f in self.findings] + [r["url"] for r in self.results]))


def tool_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "1-3 sentences: what recent public information says about the company, or 'nothing relevant found'"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "fact": {"type": "string", "description": "one concrete, dated fact if possible (news, expansion, hiring, AI initiative)"},
                        "url": {"type": "string", "description": "the search result URL that states it"},
                        "title": {"type": "string"},
                        "about": {"type": "string", "description": "'company' or the exact name of the person the fact is about"},
                    },
                    "required": ["fact", "url"],
                },
                "description": f"at most {MAX_FINDINGS}, only about THIS company",
            },
        },
        "required": ["summary", "findings"],
    }  # fmt: skip


def server_tool(max_uses: int) -> dict[str, Any]:
    settings = get_settings()
    return {"type": settings.research_web_search_tool, "name": "web_search", "max_uses": int(max_uses)}


def _row(resp: Any, now: datetime, enquiry_id: int | None) -> LlmCall:
    return LlmCall(
        feature="research", model=resp.model[:100], prompt_version=PROMPT_VERSION, enquiry_id=enquiry_id,
        input_tokens=resp.input_tokens, output_tokens=resp.output_tokens, cost_usd_micros=resp.cost_usd_micros,
        latency_ms=resp.latency_ms, status="ok", request_id=resp.request_id[:100] if resp.request_id else None,
        web_search_requests=resp.web_search_requests, created_at=now,
    )  # fmt: skip


def _error_row(exc: Exception, now: datetime, enquiry_id: int | None) -> LlmCall:
    return LlmCall(
        feature="research", model=get_settings().llm_model, prompt_version=PROMPT_VERSION, enquiry_id=enquiry_id,
        status="error", error_type=str(getattr(exc, "error_type", type(exc).__name__))[:100], created_at=now,
    )  # fmt: skip


def search_web(
    db: Session,
    llm: LLMClient | None,
    *,
    company_name: str,
    website_url: str | None,
    context_text: str = "",
    max_uses: int = 3,
    enquiry_id: int | None = None,
    people: list[str] | None = None,
) -> WebFindings:
    """One request; one ``llm_calls`` row (feature research, ``web_search_requests`` set). Flushes, never commits.

    ``LLMAuthError`` / ``LLMModelNotFound`` propagate (error row added). Any other LLM failure returns
    ``status="failed"`` so the research run continues without web results."""
    client = llm or get_llm_client()
    user = _env().get_template("research_web_user.md.j2").render(
        company_name=company_name, website_url=website_url or "(none)", context_text=context_text[:1500],
        max_uses=max_uses, max_findings=MAX_FINDINGS, people=(people or [])[:2],
    )  # fmt: skip
    req = StructuredRequest(
        feature="research",
        system=(PROMPTS_DIR / "research_web_system.md").read_text(encoding="utf-8").strip() + "\n",
        messages=[{"role": "user", "content": user.strip() + "\n"}],
        tool_name=TOOL_NAME,
        tool_description="Record what the web searches found about the company (call exactly once, after searching).",
        input_schema=tool_schema(),
        max_tokens=1200,
        prompt_version=PROMPT_VERSION,
        server_tools=[server_tool(max_uses)],
        tool_choice="auto",
        cache_system=False,  # one-off request: no cache write over the search results
        log_context={"enquiry_id": enquiry_id, "input_summary": f"web search {company_name[:60]}"},
    )
    now = datetime.now(UTC)
    try:
        resp = client.structured(req)
    except FATAL_LLM_ERRORS as exc:
        db.add(_error_row(exc, now, enquiry_id))
        db.flush()
        raise
    except LLMError as exc:
        db.add(_error_row(exc, now, enquiry_id))
        db.flush()
        log.warning("research_web_failed", error=type(exc).__name__)
        return WebFindings(status="failed", error=f"Web search unavailable ({getattr(exc, 'error_type', type(exc).__name__)})")
    db.add(_row(resp, now, enquiry_id))
    db.flush()
    results = [r for r in resp.web_results if isinstance(r.get("url"), str)]
    known = {r["url"] for r in results}
    data = resp.tool_input or {}
    findings: list[WebFinding] = []
    for item in data.get("findings") or []:
        if not isinstance(item, dict):
            continue
        url, fact = str(item.get("url") or "").strip(), " ".join(str(item.get("fact") or "").split())
        # Anti-hallucination: a finding must cite a URL the search really returned.
        if fact and url in known:
            about = " ".join(str(item.get("about") or "company").split())[:120] or "company"
            findings.append(WebFinding(fact=fact[:400], url=url[:1000], title=str(item.get("title") or "")[:300], about=about))
        if len(findings) >= MAX_FINDINGS:
            break
    summary = " ".join(str(data.get("summary") or resp.text or "").split())[:800]
    status = "done" if findings else "empty"
    log.info("research_web_done", searches=resp.web_search_requests, results=len(results), findings=len(findings))
    return WebFindings(
        status=status, findings=findings, summary=summary, results=results[:20], searches=resp.web_search_requests,
        cost_usd_micros=resp.cost_usd_micros,
    )  # fmt: skip


__all__ = ["PROMPT_VERSION", "WebFinding", "WebFindings", "search_web", "server_tool"]
