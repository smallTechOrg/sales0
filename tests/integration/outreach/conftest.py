"""Outreach fixtures: team members with (fake) Gmail connections on a migrated SQLite DB."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.config import reset_settings
from sales0.db.engine import get_sessionmaker
from sales0.db.models import Deck, Offering, OutreachMessage, TeamMember
from sales0.integrations.gmail.auth import SCOPES, StoredToken
from sales0.integrations.gmail.client import FakeGmailClient
from sales0.services.outreach import decks
from sales0.services.team import connections, members

REFRESH = "1//outreach-refresh-token-SECRET-QQQ"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@dataclass
class Team:
    """Two members (+ one admin) with fake Gmail clients keyed by member id."""

    ann: TeamMember
    bob: TeamMember
    admin: TeamMember
    gmail: dict[int, FakeGmailClient] = field(default_factory=dict)


@pytest.fixture
def db(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    monkeypatch.setenv("SALES0_SECRET_KEY", "test-secret-key-0123456789abcdef")
    monkeypatch.setenv("SALES0_ADMIN_EMAIL", "admin@team.example")
    reset_settings()
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def team(db: Session) -> Team:
    admin = members.seed_admin(db)
    ann = members.add_member(db, "Ann Rao", "ann@team.example")
    bob = members.add_member(db, "Bob Das", "bob@team.example")
    db.commit()
    t = Team(ann=ann, bob=bob, admin=admin)
    for m in (ann, bob, admin):
        connections.store_token(db, m.id, StoredToken("cid", "csecret", REFRESH, list(SCOPES), m.email))
        t.gmail[m.id] = FakeGmailClient(email=m.email)
    db.commit()
    connections.set_client_factory(lambda member_id, http: t.gmail[member_id])
    return t


@pytest.fixture
def offering(db: Session) -> Offering:
    """The seeded "AI corporate workshops" offering (migration 0002)."""
    return db.execute(select(Offering).where(Offering.name == "AI corporate workshops")).scalar_one()


@pytest.fixture
def deck(db: Session) -> Deck:
    row = decks.upload_deck(db, filename="Workshops Deck.pdf", data=PDF)
    db.commit()
    return row


def make_message(
    db: Session, member: TeamMember, *, to: str = "p@acme.example", subject: str = "Workshop for Acme", body: str = "Hello Priya,\n\nWe run AI workshops.\n\nBest regards,\nAnn Rao", deck_id: int | None = None, state: str = "draft"
) -> OutreachMessage:
    msg = OutreachMessage(
        member_id=member.id, connection_email=member.email, from_addr=member.email, to_addrs=to, subject=subject,
        body=body, deck_id=deck_id, state=state,
    )  # fmt: skip
    db.add(msg)
    db.commit()
    return msg
