"""Prospect research: one button press = one AI run = one new research version (``prospect_docs``).

Sources, all optional except the company name:
1. the company website (the existing bounded ReAct loop over the SSRF-guarded ``SafeFetcher``);
2. our own data (``context.build``: enquiries / posts, headlines, contacts, sheet rows, our notes);
3. the LinkedIn company page text the laptop worker uploaded earlier (the press also queues a fresh visit);
4. web search findings (Anthropic's server-side web search, one extra request, ``SALES0_RESEARCH_WEB_SEARCH``).

Everything runs inside the request (hosted mode has no background worker for AI): the website loop is capped at
``SALES0_RESEARCH_TOTAL_TIME_S`` (60 s) and the web step is one request, well under Cloud Run's timeout.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from sales0.agent.research import ResearchLimits, SafeFetcher, run_research
from sales0.agent.research.loop import ExtraSource
from sales0.agent.research.web import WebFindings, search_web
from sales0.config import get_settings
from sales0.db.models import CompanyBrief, Prospect, TeamMember
from sales0.errors import DomainError
from sales0.llm import LLMClient
from sales0.logging import get_logger
from sales0.services._common import clip, now_or
from sales0.services.costs_report import ensure_within_spend_cap
from sales0.services.crm import timeline
from sales0.services.crm.actor import Actor
from sales0.services.research import context, docs, linkedin_page
from sales0.services.research.docs import DocView, SourceRef
from sales0.services.triage import active_offering_refs

log = get_logger("sales0.research.prospect")

RESEARCH_SCHEMA = 3  # briefs with enquiry insights + people; older versions are re-researched by the bulk run
BRIEF_TEXT_FIELDS = ("what_they_do", "size_hints", "suggested_offering")
BRIEF_LIST_FIELDS = ("ai_training_needs", "personalisation_hooks")
FIELD_LIMITS = {"what_they_do": 1200, "size_hints": 600, "suggested_offering": 600}


def _normalise_website(url: str | None) -> str | None:
    text = (url or "").strip()
    if not text:
        return None
    return text if "://" in text else "https://" + text


def _web_source(findings: WebFindings) -> ExtraSource | None:
    if not findings.findings and not findings.summary:
        return None
    lines = [findings.summary] if findings.summary else []
    lines += [f"- {'' if f.about == 'company' else f'[about {f.about}] '}{f.fact} (source: {f.url})" for f in findings.findings]
    return ExtraSource(
        kind="web_search", label="Web search findings", url=None, text="\n".join(lines),
        citable=tuple(f.url for f in findings.findings),
    )  # fmt: skip


def _sources(brief_sources: list[str], website: str | None, linkedin: Any, findings: WebFindings | None) -> list[SourceRef]:
    web_titles = {f.url: f.title for f in (findings.findings if findings else [])}
    li_url = (linkedin.result_url or linkedin.target_url) if linkedin is not None else None
    out: list[SourceRef] = []
    for url in dict.fromkeys(brief_sources):
        if li_url and url == li_url:
            out.append(SourceRef(kind="linkedin_page", label="LinkedIn company page", url=url))
        elif url in web_titles:
            out.append(SourceRef(kind="web_search", label=web_titles[url] or url, url=url))
        else:
            out.append(SourceRef(kind="website", label=url, url=url))
    out.append(SourceRef(kind="own_data", label="Our records (enquiries, contacts, sheet, notes)"))
    return out


def research_prospect(
    db: Session,
    llm: LLMClient | None,
    member: TeamMember | None,
    prospect_id: int,
    *,
    actor: Actor,
    queue_linkedin: bool = True,
    web_search: bool | None = None,
    fetcher: SafeFetcher | None = None,
    limits: ResearchLimits | None = None,
    clock: Callable[[], float] | None = None,
    now: datetime | None = None,
    trigger: str = "manual",
) -> tuple[DocView, str]:
    """Run research for one prospect and store it as a new version (flushed; the caller commits).

    Returns (the version, a sentence about the LinkedIn page for the UI). ``LLMAuthError`` / ``LLMModelNotFound``
    propagate (error rows were added). The spend cap is checked first (``SpendCapReached``).
    ``trigger`` (manual | accepted | bulk) is recorded in the version's inputs."""
    stamp = now_or(now)
    ensure_within_spend_cap(db, stamp)
    b = context.basics(db, prospect_id)
    company_name = b.company_name or b.name
    website = _normalise_website(b.website)
    settings = get_settings()

    li_note = ""
    if queue_linkedin:
        _, li_note = linkedin_page.queue_for_prospect(db, prospect_id, b.company_id, b.company_linkedin_url, actor=actor, now=stamp)
    linkedin = linkedin_page.latest_text(db, prospect_id)
    records = context.build(db, prospect_id, detail="research")

    use_web = settings.research_web_search if web_search is None else web_search
    findings: WebFindings | None = None
    if use_web:
        findings = search_web(
            db, llm, company_name=company_name, website_url=website, context_text=records[:1500],
            max_uses=settings.research_web_search_max_uses, people=context.people_names(db, prospect_id, limit=2),
        )  # fmt: skip

    extras: list[ExtraSource] = []
    if linkedin is not None and linkedin.result_text:
        extras.append(ExtraSource(kind="linkedin_page", label="LinkedIn company page", url=linkedin.result_url or linkedin.target_url,
                                  text=linkedin.result_text[:6000]))
    web_extra = _web_source(findings) if findings else None
    if web_extra is not None:
        extras.append(web_extra)

    names = [o.name for o in active_offering_refs(db)]
    kwargs: dict[str, Any] = {"clock": clock} if clock is not None else {}
    result = run_research(
        db, llm, company_name=company_name, website_url=website, context_text=records, offerings=names,
        fetcher=fetcher, limits=limits or ResearchLimits.from_env(), extra_sources=extras, **kwargs,
    )  # fmt: skip
    data = result.brief_dict
    data.pop("sources", None)
    cost = result.cost_usd_micros + (findings.cost_usd_micros if findings else 0)
    searches = findings.searches if findings else 0
    inputs = {
        "website": website, "pages_fetched": result.pages_fetched, "own_data": True,
        "linkedin_page": linkedin.finished_at.isoformat() if linkedin is not None and linkedin.finished_at else None,
        "web_search": (findings.status if findings else "off"), "web_searches": searches,
        "web_results": [r["url"] for r in (findings.results if findings else [])][:10],
        "llm_calls": result.llm_calls + (1 if findings else 0), "prompt_version": result.prompt_version,
        # what this run covered: the bulk / auto runs re-research when an enquiry or contact is not in these lists
        "schema": RESEARCH_SCHEMA, "enquiry_ids": context.active_enquiry_ids(db, prospect_id),
        "contact_ids": context.contact_ids(db, prospect_id), "trigger": trigger,
    }  # fmt: skip
    row = CompanyBrief(
        company_name=clip(company_name, 200) or "unknown", website=clip(website, 300),
        brief_json=json.dumps({**data, "sources": result.sources, "research_status": result.status,
                               "research_error": result.error}, ensure_ascii=False),
        sources_json=json.dumps(result.sources, ensure_ascii=False), cost_usd_micros=cost, prospect_id=prospect_id,
        created_at=stamp,
    )  # fmt: skip
    db.add(row)
    db.flush()
    version = docs.add_version(
        db, prospect_id, "research", origin="ai", actor=actor, status=result.status, error=result.error, data=data,
        sources=_sources(result.sources, website, linkedin, findings), inputs=inputs, cost_usd_micros=cost,
        web_search_requests=searches, company_brief_id=row.id, now=stamp,
    )  # fmt: skip
    timeline.add_event(
        db, subject_type="prospect", subject_id=prospect_id, kind="research",
        body=f"Research version {version.version} ({result.status})", data={"doc_id": version.id, "cost_usd_micros": cost},
        actor=actor, occurred_at=stamp,
    )  # fmt: skip
    log.info("prospect_researched", prospect_id=prospect_id, version=version.version, status=result.status, cost=cost,
             searches=searches, member_id=member.id if member else None)
    return version, li_note


def _lines(value: str | None, limit: int, max_items: int) -> list[str]:
    out = []
    for line in (value or "").replace("\r\n", "\n").split("\n"):
        text = " ".join(line.strip().lstrip("-*•").split())
        if text:
            out.append(text[:limit])
    return out[:max_items]


def edit_research(
    db: Session,
    prospect_id: int,
    *,
    actor: Actor,
    what_they_do: str,
    size_hints: str = "",
    suggested_offering: str = "",
    ai_training_needs: str = "",
    personalisation_hooks: str = "",
    sources: str = "",
    enquiry_insights: str | None = None,
    now: datetime | None = None,
) -> DocView:
    """A human edit = a new version (origin human) built on the current one; the AI fields not in the form
    (company name, matched offering) are carried over."""
    docs.require_prospect(db, prospect_id)
    current = docs.latest(db, prospect_id, "research")
    base: dict[str, Any] = dict(current.data) if current is not None else {}
    text = " ".join((what_they_do or "").split())
    if not text:
        raise DomainError("research_invalid", "Say what the company does (or write 'unknown').", 400)
    base.update({
        "what_they_do": text[:FIELD_LIMITS["what_they_do"]],
        "size_hints": " ".join(size_hints.split())[:600] or "unknown",
        "suggested_offering": " ".join(suggested_offering.split())[:600] or "unknown",
        "ai_training_needs": _lines(ai_training_needs, 300, 6),
        "personalisation_hooks": _lines(personalisation_hooks, 300, 4),
    })  # fmt: skip
    if enquiry_insights is not None:  # the form field is absent for old callers: keep what the version had
        base["enquiry_insights"] = _lines(enquiry_insights, 300, 4)
    p = db.get(Prospect, prospect_id)
    base.setdefault("company_name", p.name if p else "unknown")
    kept = {s.url: s for s in (current.sources if current else []) if s.url}
    refs: list[SourceRef] = []
    for url in _lines(sources, 1000, 20):
        if not url.startswith(("http://", "https://")):
            raise DomainError("research_invalid", "Sources must be web links (http:// or https://), one per line.", 400)
        refs.append(kept.get(url) or SourceRef(kind="website", label=url, url=url))
    refs += [s for s in (current.sources if current else []) if s.url is None]
    return docs.add_version(
        db, prospect_id, "research", origin="human", actor=actor, status="done", data=base, sources=refs,
        inputs={**{k: v for k, v in (current.inputs if current else {}).items() if k in ("schema", "enquiry_ids", "contact_ids")},
                "edited_from": current.version if current else None}, based_on_version=current.version if current else None,
        now=now,
    )  # fmt: skip


def brief_for_drafting(db: Session, prospect_id: int | None, doc_id: int | None = None) -> tuple[dict[str, Any] | None, DocView | None]:
    """(brief dict as ``generate_draft(brief=...)`` takes it, the version) - an explicit version, else the newest
    usable one (a human edit included)."""
    if prospect_id is None and doc_id is None:
        return None, None
    doc = docs.get(db, doc_id) if doc_id else None
    if doc is not None and (doc.doc_type != "research" or (prospect_id is not None and doc.prospect_id != prospect_id)):
        doc = None
    if doc is None and prospect_id is not None:
        doc = docs.latest_usable(db, prospect_id, "research")
    if doc is None:
        return None, None
    brief = dict(doc.data)
    brief["sources"] = [s.url for s in doc.sources if s.url]
    brief["research_status"] = doc.status
    brief["research_version"] = doc.version
    return brief, doc


__all__ = ["brief_for_drafting", "edit_research", "research_prospect"]
