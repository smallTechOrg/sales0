"""Pipeline read models: summary counts and the board."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from sales0.db.models import Client, Company, Flag, Lead, Prospect
from sales0.db.models.enums_crm import LEAD_STAGES, PROSPECT_STAGES
from sales0.services._common import now_or
from sales0.services.crm import _keys, followups
from sales0.services.crm._common import invalid, today_local
from sales0.services.crm.stages import STAGE_LABELS
from sales0.services.crm.views import Board, BoardColumn, PipelineSummary


def summary(db: Session, *, today: date | None = None, now: datetime | None = None) -> PipelineSummary:
    today = today or today_local(now)
    stamp = now_or(now)
    by_stage = dict(db.execute(select(Prospect.stage, func.count()).where(Prospect.status == "open").group_by(Prospect.stage)).all())
    lead_stage = dict(db.execute(select(Lead.stage, func.count()).where(Lead.closed_at.is_(None)).group_by(Lead.stage)).all())
    recent = dict(
        db.execute(
            select(Lead.stage, func.count()).where(Lead.closed_at.is_not(None), Lead.closed_at >= stamp - timedelta(days=90)).group_by(Lead.stage)
        ).all()
    )
    client_counts = dict(db.execute(select(Client.status, func.count()).group_by(Client.status)).all())
    panel = followups.list_due(db, today=today)
    return PipelineSummary(
        prospects_by_stage={s: int(by_stage.get(s, 0)) for s in PROSPECT_STAGES},
        leads_by_stage={s: int(lead_stage.get(s, 0)) for s in LEAD_STAGES if s in ("meeting_scheduled", "proposal_sent")},
        closed_recent={s: int(recent.get(s, 0)) for s in ("won", "lost", "no_response")},
        clients_active=int(client_counts.get("active", 0)),
        clients_legacy=int(client_counts.get("legacy", 0)),
        due_today=len(panel.due_today),
        overdue=len(panel.overdue),
    )


def board(
    db: Session, *, kind: str = "prospects", q: str | None = None, flag: str | None = None, per_column: int = 50,
    now: datetime | None = None,
) -> Board:
    from sales0.services.crm import leads, prospects

    if kind not in ("prospects", "leads"):
        raise invalid("stage_invalid", "The board shows prospects or leads.")
    stamp = now_or(now)
    like = f"%{_keys.like_escape(_keys.name_key(q))}%" if q and q.strip() else None
    columns: list[BoardColumn] = []
    if kind == "prospects":
        for stage in PROSPECT_STAGES:
            stmt = select(Prospect).where(Prospect.status == "open", Prospect.stage == stage)
            if like:
                stmt = stmt.where(or_(func.lower(Prospect.name).like(like, escape="\\"),
                                      Prospect.company_id.in_(select(Company.id).where(Company.name_key.like(like, escape="\\")))))
            if flag:
                stmt = stmt.where(exists().where(Flag.entity_type == "prospect", Flag.entity_id == Prospect.id, Flag.kind == flag, Flag.cleared_at.is_(None)))
            total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            rows = list(db.scalars(stmt.order_by(Prospect.updated_at.desc(), Prospect.id.desc()).limit(per_column)))
            columns.append(BoardColumn(stage=stage, label=STAGE_LABELS[stage], total=total, cards=prospects.views_for(db, rows)))
    else:
        for stage in LEAD_STAGES:
            stmt = select(Lead).join(Prospect, Prospect.id == Lead.prospect_id).where(Lead.stage == stage)
            if stage in ("won", "lost", "no_response"):
                stmt = stmt.where(Lead.closed_at.is_not(None), Lead.closed_at >= stamp - timedelta(days=90))
            else:
                stmt = stmt.where(Lead.closed_at.is_(None))
            if like:
                stmt = stmt.where(or_(func.lower(Prospect.name).like(like, escape="\\"),
                                      Prospect.company_id.in_(select(Company.id).where(Company.name_key.like(like, escape="\\")))))
            if flag:
                stmt = stmt.where(exists().where(Flag.entity_type == "prospect", Flag.entity_id == Lead.prospect_id, Flag.kind == flag, Flag.cleared_at.is_(None)))
            total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            rows = list(db.scalars(stmt.order_by(Lead.updated_at.desc(), Lead.id.desc()).limit(per_column)))
            columns.append(BoardColumn(stage=stage, label=STAGE_LABELS[stage], total=total, cards=leads.views_for(db, rows, now=stamp)))
    return Board(kind=kind, columns=columns)


__all__ = ["board", "summary"]
