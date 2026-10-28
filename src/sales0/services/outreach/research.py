"""Company research for outreach: run the research agent and persist the brief (with its cost)."""

from __future__ import annotations

import json
from collections.abc import Callable

from sqlalchemy.orm import Session

from sales0.agent.research import ResearchLimits, SafeFetcher, run_research
from sales0.db.models import CompanyBrief, Enquiry, TeamMember
from sales0.llm import LLMClient
from sales0.logging import get_logger
from sales0.services._common import clip
from sales0.services.triage import active_offering_refs

log = get_logger("sales0.outreach.research")


def research_company(
    db: Session,
    llm: LLMClient | None,
    member: TeamMember,
    *,
    company_name: str,
    website_url: str | None,
    context_text: str = "",
    enquiry_id: int | None = None,
    prospect_id: int | None = None,
    limits: ResearchLimits | None = None,
    fetcher: SafeFetcher | None = None,
    clock: Callable[[], float] | None = None,
) -> CompanyBrief:
    """Research one company and store a ``company_briefs`` row (flushed; the caller commits).

    ``brief_json`` is the agent's brief plus ``research_status`` (done|partial|failed) and
    ``research_error``. Cost is the sum of the agent's Anthropic calls. LLMAuthError /
    LLMModelNotFound propagate (the agent has already added an error ``llm_calls`` row).
    """
    names = [o.name for o in active_offering_refs(db)]
    ref_enquiry = enquiry_id if enquiry_id is not None and db.get(Enquiry, enquiry_id) is not None else None
    kwargs = {"clock": clock} if clock is not None else {}
    result = run_research(
        db,
        llm,
        company_name=company_name,
        website_url=website_url,
        context_text=context_text,
        offerings=names,
        fetcher=fetcher,
        limits=limits or ResearchLimits.from_env(),
        enquiry_id=ref_enquiry,
        **kwargs,
    )
    payload = result.brief_dict
    payload["research_status"] = result.status
    payload["research_error"] = result.error
    row = CompanyBrief(
        company_name=clip(company_name, 200) or result.brief.company_name[:200],
        website=clip(website_url, 300),
        brief_json=json.dumps(payload, ensure_ascii=False),
        sources_json=json.dumps(result.sources, ensure_ascii=False),
        cost_usd_micros=result.cost_usd_micros,
        enquiry_id=enquiry_id,
        prospect_id=prospect_id,
    )
    db.add(row)
    db.flush()
    log.info("company_researched", brief_id=row.id, member_id=member.id, status=result.status, cost=result.cost_usd_micros)
    return row


def brief_dict(row: CompanyBrief) -> dict[str, object]:
    """The stored brief as a dict (what ``generate_draft(brief=...)`` takes)."""
    return json.loads(row.brief_json)
