"""ReAct research loop: Haiku picks fetch_url | finish each step (forced tool use), code enforces every cap."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import ValidationError
from sqlalchemy.orm import Session

from sales0.agent.research.extract import PageText, extract_html, extract_plain
from sales0.agent.research.fetch import FetchError, SafeFetcher
from sales0.agent.research.schemas import (
    CompanyBrief,
    ResearchAction,
    ResearchLimits,
    ResearchResult,
    StepRecord,
    action_tool_schema,
)
from sales0.agent.research.site import same_site
from sales0.config import get_settings
from sales0.db.models import LlmCall
from sales0.llm import (
    LLMAuthError,
    LLMBadResponse,
    LLMClient,
    LLMError,
    LLMModelNotFound,
    get_llm_client,
)
from sales0.llm.client import StructuredRequest
from sales0.logging import get_logger

log = get_logger("sales0.agent.research")

PROMPT_VERSION = "research-v3"  # bump on any prompt or schema edit (v3: enquiry insights + people)
TOOL_NAME = "research_step"
TOOL_DESCRIPTION = (
    "Choose the next research action: fetch_url (one page on the company's own site) or finish "
    "(deliver the final brief, citing only fetched URLs)."
)
FATAL_LLM_ERRORS = (LLMAuthError, LLMModelNotFound)
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def inert(value: object) -> str:
    """Neutralise delimiter breakouts in untrusted text."""
    return str(value).replace("<", "&lt;").replace(">", "&gt;")


@lru_cache(maxsize=1)
def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(PROMPTS_DIR),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    env.filters["inert"] = inert
    return env


def render_system() -> str:
    return (PROMPTS_DIR / "research_system.md").read_text(
        encoding="utf-8"
    ).strip() + "\n"


def render_user(**ctx: Any) -> str:
    ctx.setdefault("extras", [])
    return _env().get_template("research_user.md.j2").render(**ctx).strip() + "\n"


SPLIT_MARKER = "\n<!-- volatile -->\n"


def user_content(text: str, *, cache: bool) -> str | list[dict[str, Any]]:
    """The user turn. The stable head (task, records, other sources, offerings) comes first so that, on a
    multi-step run, it can be a cached prefix; the per-step tail (pages so far, budget, notes, links) follows."""
    head, sep, tail = text.partition(SPLIT_MARKER)
    if not sep:
        return text
    if not cache:
        return head.rstrip() + "\n\n" + tail.lstrip()
    return [
        {"type": "text", "text": head.rstrip() + "\n", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": tail.lstrip() or "(continue)"},
    ]


@dataclass(frozen=True)
class ExtraSource:
    """Text the research agent did not fetch itself: the LinkedIn company page (laptop worker) or web search
    findings. Always untrusted DATA. ``url`` may be cited as a source."""

    kind: str  # linkedin_page | web_search
    label: str
    url: str | None
    text: str
    citable: tuple[str, ...] = ()


def _normalise_start_url(website_url: str | None) -> str | None:
    if not website_url or not website_url.strip():
        return None
    url = website_url.strip()
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    return url if parts.scheme in ("http", "https") and parts.hostname else None


def _llm_row(resp: Any, enquiry_id: int | None, now: datetime) -> LlmCall:
    return LlmCall(
        feature="research",
        model=resp.model[:100],
        prompt_version=PROMPT_VERSION,
        enquiry_id=enquiry_id,
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
        cost_usd_micros=resp.cost_usd_micros,
        latency_ms=resp.latency_ms,
        status="ok",
        request_id=resp.request_id[:100] if resp.request_id else None,
        created_at=now,
    )


def _error_row(exc: Exception, enquiry_id: int | None, now: datetime) -> LlmCall:
    return LlmCall(
        feature="research",
        model=get_settings().llm_model,
        prompt_version=PROMPT_VERSION,
        enquiry_id=enquiry_id,
        status="error",
        error_type=str(getattr(exc, "error_type", type(exc).__name__))[:100],
        created_at=now,
    )


def _finalise_brief(
    brief: CompanyBrief, fetched: list[str], offering_names: list[str], citable: list[str] | None = None
) -> CompanyBrief:
    fetched_set = set(fetched) | set(citable or [])
    sources = [s for s in dict.fromkeys(brief.sources) if s in fetched_set] or list(
        fetched
    )
    matched = None
    if brief.matched_offering_name:
        by_lower = {n.lower(): n for n in offering_names}
        matched = by_lower.get(brief.matched_offering_name.strip().lower())
    return brief.model_copy(
        update={
            "sources": sources,
            "matched_offering_name": matched,
            "personalisation_hooks": brief.personalisation_hooks[:3],
            "enquiry_insights": brief.enquiry_insights[:4],
            "people": brief.people[:3],
            "limited_information": brief.limited_information or not (fetched or citable),
        }
    )


def _fallback_brief(company_name: str, fetched: list[str]) -> CompanyBrief:
    return CompanyBrief(
        company_name=company_name or "unknown",
        what_they_do="unknown",
        size_hints="unknown",
        suggested_offering="unknown",
        sources=list(fetched),
        limited_information=True,
    )


def run_research(
    db_session: Session,
    llm: LLMClient | None,
    *,
    company_name: str,
    website_url: str | None,
    context_text: str = "",
    offerings: list[str] | None = None,
    fetcher: SafeFetcher | None = None,
    limits: ResearchLimits | None = None,
    enquiry_id: int | None = None,
    clock: Callable[[], float] = time.monotonic,
    extra_sources: list[ExtraSource] | None = None,
) -> ResearchResult:
    """Research one company; one ``llm_calls`` row (feature="research") per Anthropic request.

    ``offerings`` are offering NAMES. Flushes; the caller commits. ``LLMAuthError`` /
    ``LLMModelNotFound`` add an error row and propagate. Other LLM failures end the loop with
    a partial/failed result (never raises for them).
    """
    limits = limits or ResearchLimits()
    offering_names = list(offerings or [])
    extras = [e for e in (extra_sources or []) if e.text.strip()]
    citable = list(dict.fromkeys(u for e in extras for u in ((e.url,) if e.url else ()) + e.citable))
    client = llm or get_llm_client()
    owns_fetcher = fetcher is None
    fetcher = fetcher or SafeFetcher(
        max_bytes=limits.max_bytes_per_page, timeout_s=limits.fetch_timeout_s
    )
    start_url = _normalise_start_url(website_url)
    deadline = clock() + limits.total_time_s
    # SafeFetcher deadlines use time.monotonic; derive one from the (possibly fake) clock budget.
    mono_deadline = time.monotonic() + limits.total_time_s

    steps: list[StepRecord] = []
    pages: list[dict[str, str]] = []  # what the model sees
    fetched: list[str] = []  # URLs (requested + final) of successfully fetched pages
    tried: set[str] = set()
    link_pool: list[str] = [start_url] if start_url else []
    notes: list[str] = []
    cost = 0
    calls = 0
    error: str | None = None
    brief: CompanyBrief | None = None

    if start_url is None:
        notes.append(
            "No usable website URL was provided: nothing can be fetched; finish with what our records and the other "
            "sources support."
        )

    def available_links() -> list[str]:
        out = []
        for u in link_pool:
            if u not in tried and u not in out:
                out.append(u)
        return out[: limits.max_links_to_model]

    try:
        for step in range(1, limits.max_steps + 1):
            page_cap = len(fetched) >= limits.max_pages
            out_of_time = clock() >= deadline
            must_finish = (
                start_url is None or page_cap or out_of_time or step == limits.max_steps
            )
            if out_of_time and not page_cap and start_url is not None:
                notes.append("The time budget is used up.")
            user = render_user(
                company_name=company_name,
                website_url=start_url or "(none)",
                context_text=context_text,
                offerings=offering_names,
                step=step,
                max_steps=limits.max_steps,
                pages_fetched=len(fetched),
                max_pages=limits.max_pages,
                must_finish=must_finish,
                notes=notes[-6:],
                pages=pages,
                links=[] if must_finish else available_links(),
                extras=[{"kind": e.kind, "label": e.label, "url": e.url or "", "text": e.text} for e in extras],
            )
            notes = []  # one-shot notes
            req = StructuredRequest(
                feature="research",
                system=render_system(),
                messages=[{"role": "user", "content": user_content(user, cache=start_url is not None)}],
                tool_name=TOOL_NAME,
                tool_description=TOOL_DESCRIPTION,
                input_schema=action_tool_schema(allow_fetch=not must_finish),
                max_tokens=limits.max_tokens,
                prompt_version=PROMPT_VERSION,
                log_context={
                    "enquiry_id": enquiry_id,
                    "step": step,
                    "input_summary": f"research {company_name[:60]}",
                },
            )
            now = datetime.now(UTC)
            try:
                resp = client.structured(req)
            except FATAL_LLM_ERRORS as exc:
                db_session.add(_error_row(exc, enquiry_id, now))
                db_session.flush()
                raise
            except LLMBadResponse as exc:
                db_session.add(_error_row(exc, enquiry_id, now))
                calls += 1
                steps.append(
                    StepRecord(
                        step,
                        "invalid",
                        observation="bad response from the model",
                        ok=False,
                    )
                )
                notes.append(
                    "Your previous answer was not usable. Call research_step again with a valid action."
                )
                continue
            except LLMError as exc:
                db_session.add(_error_row(exc, enquiry_id, now))
                calls += 1
                error = "LLM unavailable: " + str(
                    getattr(exc, "error_type", type(exc).__name__)
                )
                steps.append(StepRecord(step, "error", observation=error, ok=False))
                break
            calls += 1
            cost += resp.cost_usd_micros
            db_session.add(_llm_row(resp, enquiry_id, now))

            try:
                if resp.tool_input is None:
                    raise ValueError("no research_step tool call in the answer")
                act = ResearchAction.model_validate(resp.tool_input)
                if act.action == "fetch_url" and not (act.url and act.url.strip()):
                    raise ValueError("fetch_url needs a url")
                if act.action == "finish" and act.brief is None:
                    raise ValueError("finish needs a brief")
            except (ValidationError, ValueError) as exc:
                msg = str(exc).splitlines()[0][:160]
                steps.append(StepRecord(step, "invalid", observation=msg, ok=False))
                notes.append(
                    f"Your previous answer was invalid ({msg}). Call research_step again with a valid action."
                )
                continue

            if act.action == "finish":
                assert act.brief is not None
                brief = _finalise_brief(act.brief, fetched, offering_names, citable)
                steps.append(StepRecord(step, "finish", observation="brief delivered"))
                break

            url = (act.url or "").strip()
            assert act.action == "fetch_url"
            if must_finish:
                steps.append(
                    StepRecord(
                        step,
                        "fetch_url",
                        url,
                        "refused: fetching is no longer allowed",
                        ok=False,
                    )
                )
                notes.append("Fetching is no longer allowed; call finish.")
                continue
            if url in tried:
                steps.append(
                    StepRecord(
                        step, "fetch_url", url, "refused: already fetched", ok=False
                    )
                )
                notes.append(
                    f"{url} was already requested; choose another page or finish."
                )
                continue
            tried.add(url)
            assert start_url is not None
            site_root = start_url

            def on_site(u: str) -> bool:
                return same_site(u, site_root)

            if not on_site(url):
                steps.append(
                    StepRecord(
                        step, "fetch_url", url, "refused: off-site URL", ok=False
                    )
                )
                notes.append(
                    f"Refused {url}: only the company's own site may be fetched."
                )
                continue
            try:
                result = fetcher.fetch(
                    url,
                    deadline=mono_deadline,
                    host_ok=on_site,
                )
            except FetchError as exc:
                steps.append(
                    StepRecord(step, "fetch_url", url, f"failed: {exc}", ok=False)
                )
                notes.append(f"fetch_url {url} failed: {exc}")
                continue
            if result.status >= 400:
                steps.append(
                    StepRecord(
                        step, "fetch_url", url, f"HTTP {result.status}", ok=False
                    )
                )
                notes.append(f"fetch_url {url} returned HTTP {result.status}.")
                continue
            page: PageText = (
                extract_plain(result.body)
                if result.content_type == "text/plain"
                else extract_html(result.body, result.final_url)
            )
            text = page.text[: limits.page_chars_to_model]
            pages.append({"url": result.final_url, "title": page.title, "text": text})
            for u in (url, result.final_url):
                if u not in fetched:
                    fetched.append(u)
            tried.add(result.final_url)
            for link in page.links:
                if same_site(link, start_url) and link not in link_pool:
                    link_pool.append(link)
            steps.append(
                StepRecord(
                    step,
                    "fetch_url",
                    url,
                    f"ok: {len(page.text)} chars"
                    + (" (truncated)" if result.truncated else ""),
                )
            )
    finally:
        if owns_fetcher:
            fetcher.close()

    page_urls = [p["url"] for p in pages]
    # `fetched` holds requested + final URLs; sources shown to users prefer the final ones.
    status: str = "done"
    if brief is None:
        status = "partial" if pages else "failed"
        error = (
            error
            or "research ended without a brief (step/time budget exhausted or invalid model output)"
        )
        brief = _fallback_brief(company_name, page_urls + citable)
    pages_fetched = len(pages)
    db_session.flush()
    log.info(
        "research_done",
        status=status,
        steps=len(steps),
        pages=pages_fetched,
        cost_usd_micros=cost,
    )
    return ResearchResult(
        status=status,  # type: ignore[arg-type]
        brief=brief,
        sources=list(brief.sources),
        steps=steps,
        cost_usd_micros=cost,
        llm_calls=calls,
        pages_fetched=pages_fetched,
        error=error,
        prompt_version=PROMPT_VERSION,
        pages=pages,
    )
