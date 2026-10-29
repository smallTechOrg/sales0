"""Every mutating entry point carries the acting member (actor_user_id / credited_by / timeline actor).

1. ``test_every_mutating_route_is_classified`` enumerates ALL POST/PUT/PATCH/DELETE routes of the real app:
   each must depend on the acting-member resolution, or be listed in EXEMPT with a reason. A new route
   that forgets the actor fails here.
2. The behavioural tests drive the main flows as a logged-in member and read the audit columns back.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sales0.db.engine import session_scope
from sales0.db.models import (
    Competitor,
    Contact,
    Enquiry,
    Keyword,
    Prospect,
    StatusHistory,
    TeamMember,
    TimelineEvent,
    TriageDecision,
)
from sales0.web import acting
from sales0.web.app import create_app
from tests.integration.members.conftest import activate, add_member
from tests.integration.web.conftest import make_enquiry, make_seen_post, triaged

ACTOR_DEPS = {acting.get_acting_member, acting.get_acting_member_optional, acting.require_admin, acting.get_login_member_optional}

# route -> why it carries no per-member attribution
# (setup/*, schedule/*, sync/* and run control are admin-only now: require_admin counts as the actor dependency)
EXEMPT_PREFIX = {
    "/login": "authentication itself",
    "/logout": "authentication itself",
    "/set-password/": "authenticated by the one-time token",
    "/api/worker/": "bearer-token API of the laptop worker (SALES0_WORKER_TOKEN); not a member action",
    "/inbox/{enquiry_id}/retry": "re-runs AI triage on an enquiry; no decision",
    "/digest/build": "builds a derived digest",
    "/suggestions/generate": "AI suggestion batch; decisions (accept/dismiss) are attributed",
    "/metrics/targets": "per-source target override stored in app_meta (shared setting, no actor column)",
    "/tracked/{profile_id}/pause": "pauses/resumes the watch of one profile (state on the profile row, no actor column)",
}


def _walk(router):  # type: ignore[no-untyped-def]
    for r in router.routes:
        if hasattr(r, "original_router"):
            yield from _walk(r.original_router)
        elif isinstance(r, APIRoute):
            yield r


def _deps(dep, out):  # type: ignore[no-untyped-def]
    for sub in dep.dependencies:
        out.add(sub.call)
        _deps(sub, out)
    return out


def test_every_mutating_route_is_classified(db_url: str) -> None:
    app = create_app()
    unclassified, stale = [], set(EXEMPT_PREFIX)
    seen = 0
    for r in _walk(app.router):
        if not set(r.methods) & {"POST", "PUT", "PATCH", "DELETE"}:
            continue
        seen += 1
        if _deps(r.dependant, set()) & ACTOR_DEPS:
            continue
        hit = [p for p in EXEMPT_PREFIX if r.path == p or (p.endswith("/") and r.path.startswith(p)) or r.path.startswith(p + "/") or r.path == p]
        if hit:
            stale -= set(hit)
        else:
            unclassified.append(r.path)
    assert seen > 80
    assert not unclassified, f"mutating routes without an acting member or an EXEMPT reason: {unclassified}"
    assert not stale, f"EXEMPT entries that match no route (remove them): {sorted(stale)}"


# ---------------------------------------------------------------------- behaviour
def _member(admin: TestClient, make_client: Callable[[], TestClient]) -> tuple[TestClient, int]:
    client = activate(make_client, add_member(admin, "Meera Member", "meera@acct.example"), "meera@acct.example")
    with session_scope() as db:
        return client, db.scalar(select(TeamMember.id).where(TeamMember.email == "meera@acct.example"))


def test_keywords_runs_and_enquiry_edits_carry_the_member(admin: TestClient, make_client: Callable[[], TestClient]) -> None:
    member, mid = _member(admin, make_client)
    # keywords are admin-only: the admin acts as the member, so the credit still lands on the member
    assert admin.post("/team/acting", data={"member_id": mid, "next": "/setup"}).status_code == 303
    assert member.post("/setup/keywords", data={"text": "nope"}).status_code == 403
    assert admin.post("/setup/keywords", data={"text": "audit keyword"}).status_code == 303
    with session_scope() as db:
        kw = db.scalar(select(Keyword).where(Keyword.text == "audit keyword"))
        assert kw.actor_user_id == mid
        kid = kw.id
        kw.actor_user_id = None
    admin.post(f"/setup/keywords/{kid}/toggle", data={"active": "false"})
    with session_scope() as db:
        assert db.get(Keyword, kid).actor_user_id == mid
        db.get(Keyword, kid).actor_user_id = None
    admin.post(f"/setup/keywords/{kid}/edit", data={"text": "audit keyword two"})
    with session_scope() as db:
        assert db.get(Keyword, kid).actor_user_id == mid

    eid = make_enquiry("P1", **triaged("genuine", "Looks real", priority="medium"))
    member.post(f"/inbox/{eid}/fields", data={"priority": "high"})
    with session_scope() as db:
        h = db.scalars(select(StatusHistory).where(StatusHistory.entity_id == eid, StatusHistory.field == "priority")).one()
        assert h.actor_user_id == mid


def test_discard_and_skipped_override_carry_the_member(admin: TestClient, make_client: Callable[[], TestClient]) -> None:
    member, mid = _member(admin, make_client)
    eid = make_enquiry("P1", **triaged("genuine", "Looks real"))
    member.post("/inbox/decide", data={"enquiry_ids": str(eid), "decision": "discard", "discard_reason": "not for us"})
    with session_scope() as db:
        assert db.get(Enquiry, eid).actor_user_id == mid
        d = db.scalars(select(TriageDecision).where(TriageDecision.enquiry_id == eid)).one()
        assert d.actor_user_id == mid and d.decision == "discard"
    sid = make_seen_post()
    member.post(f"/inbox/skipped/{sid}/override")
    with session_scope() as db:
        e = db.scalars(select(Enquiry).where(Enquiry.actor_user_id == mid, Enquiry.id != eid)).one()
        assert e.review_status == "accepted" and e.prospect_id  # the CRM prospect was created for the member, too
        p = db.get(Prospect, e.prospect_id)
        assert p.actor_member_id == mid and p.actor == "Meera Member"


def test_pipeline_contacts_and_registries_carry_the_member(admin: TestClient, make_client: Callable[[], TestClient]) -> None:
    member, mid = _member(admin, make_client)
    r = member.post("/pipeline/prospects", data={"name": "Acme Buyer", "company": "Acme"})
    cid = int(re.search(r"/companies/(\d+)", r.headers["location"]).group(1))
    with session_scope() as db:
        pid = db.scalar(select(Prospect.id).where(Prospect.company_id == cid))
    member.post(f"/pipeline/prospects/{pid}/stage", data={"stage": "contacted"})
    member.post(f"/pipeline/prospects/{pid}/note", data={"body": "called them"})
    member.post("/contacts", data={"full_name": "Ravi Contact", "email": "ravi@acme.example"})
    assert member.post("/competitors/competitor", data={"name": "RivalCo"}).status_code == 403  # admin-only
    assert admin.post("/team/acting", data={"member_id": mid, "next": "/competitors"}).status_code == 303
    admin.post("/competitors/competitor", data={"name": "RivalCo"})  # admin acting as the member: credit = member
    with session_scope() as db:
        events = db.scalars(select(TimelineEvent).where(TimelineEvent.prospect_id == pid)).all()
        by_kind = {t.kind: t for t in events}
        assert {"stage_change", "note"} <= set(by_kind)
        for kind in ("stage_change", "note"):
            assert by_kind[kind].actor == "Meera Member" and by_kind[kind].actor_member_id == mid
        assert db.get(Prospect, pid).actor_member_id == mid
        assert db.scalar(select(func.count(Contact.id))) >= 1
        rival = db.scalar(select(Competitor).where(Competitor.name == "RivalCo"))
        assert rival is not None
        created = db.scalars(select(TimelineEvent).where(TimelineEvent.subject_type == "competitor", TimelineEvent.subject_id == rival.id)).all()
        assert created and all(t.actor_member_id == mid for t in created)
