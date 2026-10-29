"""Versioned prospect documents (``prospect_docs``): research briefs and account summaries.

Every AI run and every human edit appends a version (1, 2, ...); nothing is overwritten, the highest version is
current, and drafting reads the current one (so a human edit wins until the next AI run). ``origin`` says who
wrote it: ``ai`` (an AI run) or ``human`` (an edit, ``based_on_version`` = the version it started from).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sales0.db.models import Prospect, ProspectDoc
from sales0.errors import DomainError
from sales0.services._common import now_or
from sales0.services.crm.actor import Actor

HISTORY_LIMIT = 50


class SourceRef(BaseModel):
    kind: str  # website | linkedin_page | web_search | own_data
    label: str
    url: str | None = None


class DocView(BaseModel):
    id: int
    prospect_id: int
    doc_type: str
    version: int
    origin: str  # ai | human
    status: str
    error: str | None = None
    body: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    sources: list[SourceRef] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    based_on_version: int | None = None
    cost_usd_micros: int = 0
    web_search_requests: int = 0
    actor: str
    author_member_id: int | None = None
    created_at: datetime

    @property
    def origin_label(self) -> str:
        return "AI-generated" if self.origin == "ai" else f"Edited by {self.actor}"


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def view(row: ProspectDoc) -> DocView:
    sources = []
    for item in _loads(row.sources_json, []):
        if isinstance(item, dict) and item.get("label"):
            url = item.get("url")
            sources.append(SourceRef(kind=str(item.get("kind") or "website"), label=str(item["label"])[:300],
                                     url=url if isinstance(url, str) and url.startswith(("http://", "https://")) else None))
        elif isinstance(item, str) and item.startswith(("http://", "https://")):
            sources.append(SourceRef(kind="website", label=item[:300], url=item))
    return DocView(
        id=row.id, prospect_id=row.prospect_id, doc_type=row.doc_type, version=row.version, origin=row.origin,
        status=row.status, error=row.error, body=row.body, data=_loads(row.data_json, {}), sources=sources,
        inputs=_loads(row.inputs_json, {}), based_on_version=row.based_on_version, cost_usd_micros=row.cost_usd_micros,
        web_search_requests=row.web_search_requests, actor=row.actor, author_member_id=row.author_member_id,
        created_at=row.created_at,
    )  # fmt: skip


def require_prospect(db: Session, prospect_id: int) -> Prospect:
    p = db.get(Prospect, prospect_id)
    if p is None:
        raise DomainError("not_found", "That prospect no longer exists - refresh the page.", 404)
    return p


def add_version(
    db: Session,
    prospect_id: int,
    doc_type: str,
    *,
    origin: str,
    actor: Actor,
    status: str = "done",
    error: str | None = None,
    body: str | None = None,
    data: dict[str, Any] | None = None,
    sources: list[SourceRef] | None = None,
    inputs: dict[str, Any] | None = None,
    based_on_version: int | None = None,
    cost_usd_micros: int = 0,
    web_search_requests: int = 0,
    company_brief_id: int | None = None,
    now: datetime | None = None,
) -> DocView:
    """Append the next version (flushed). Concurrent writers of the same prospect + type lose on the unique
    (prospect_id, doc_type, version) and get a 409 to retry."""
    require_prospect(db, prospect_id)
    latest = db.scalar(
        select(func.max(ProspectDoc.version)).where(ProspectDoc.prospect_id == prospect_id, ProspectDoc.doc_type == doc_type)
    )
    row = ProspectDoc(
        prospect_id=prospect_id, doc_type=doc_type, version=int(latest or 0) + 1, origin=origin, status=status,
        error=(error or None) and error[:500], body=body,
        data_json=json.dumps(data, ensure_ascii=False) if data is not None else None,
        sources_json=json.dumps([s.model_dump() for s in sources or []], ensure_ascii=False),
        inputs_json=json.dumps(inputs or {}, ensure_ascii=False), based_on_version=based_on_version,
        cost_usd_micros=int(cost_usd_micros), web_search_requests=int(web_search_requests),
        company_brief_id=company_brief_id, author_member_id=actor.member_id, actor=actor.label[:80],
        created_at=now_or(now),
    )  # fmt: skip
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        text = str(exc.orig).lower()
        if "uq_prospect_docs_version" in text or "unique" in text:  # a concurrent writer took this version number
            raise DomainError("version_conflict", "Someone saved a newer version at the same time - reload and try again.", 409) from exc
        raise
    return view(row)


def latest(db: Session, prospect_id: int, doc_type: str) -> DocView | None:
    row = db.scalar(
        select(ProspectDoc)
        .where(ProspectDoc.prospect_id == prospect_id, ProspectDoc.doc_type == doc_type)
        .order_by(ProspectDoc.version.desc())
        .limit(1)
    )
    return view(row) if row is not None else None


def latest_usable(db: Session, prospect_id: int, doc_type: str) -> DocView | None:
    """The newest version that is not a failed AI run (what drafting reads)."""
    row = db.scalar(
        select(ProspectDoc)
        .where(ProspectDoc.prospect_id == prospect_id, ProspectDoc.doc_type == doc_type, ProspectDoc.status != "failed")
        .order_by(ProspectDoc.version.desc())
        .limit(1)
    )
    return view(row) if row is not None else None


def history(db: Session, prospect_id: int, doc_type: str, limit: int = HISTORY_LIMIT) -> list[DocView]:
    rows = db.scalars(
        select(ProspectDoc)
        .where(ProspectDoc.prospect_id == prospect_id, ProspectDoc.doc_type == doc_type)
        .order_by(ProspectDoc.version.desc())
        .limit(limit)
    ).all()
    return [view(r) for r in rows]


def get(db: Session, doc_id: int) -> DocView | None:
    row = db.get(ProspectDoc, doc_id)
    return view(row) if row is not None else None


__all__ = ["DocView", "SourceRef", "add_version", "get", "history", "latest", "latest_usable", "require_prospect", "view"]
