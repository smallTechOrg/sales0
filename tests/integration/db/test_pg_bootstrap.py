"""Hosted first boot against a real PostgreSQL: a role that is NOT a superuser but may CREATE DATABASE (what
``gcloud sql users create`` gives on Cloud SQL) creates ``sales0`` itself - and so owns it - then migrates it.

Runs only when ``SALES0_TEST_POSTGRES_URL`` points at a server whose user may create roles."""

from __future__ import annotations

import os
import threading

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from sales0.db.bootstrap import bootstrap, ensure_database
from sales0.db.migrate_check import current_revision, head_revision

ADMIN = os.environ.get("SALES0_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not ADMIN, reason="needs SALES0_TEST_POSTGRES_URL (a local PostgreSQL)")

ROLE, PASSWORD, DB = "sales0_app_bootstrap_t", "pw-with-@-and/slash", "sales0_bootstrap_t"


def admin_engine():
    return create_engine(ADMIN, isolation_level="AUTOCOMMIT", poolclass=NullPool)


@pytest.fixture
def role() -> str:
    admin = admin_engine()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{DB}" WITH (FORCE)'))
        c.execute(text(f'DROP ROLE IF EXISTS "{ROLE}"'))
        c.execute(text(f"CREATE ROLE \"{ROLE}\" LOGIN CREATEDB NOSUPERUSER PASSWORD '{PASSWORD}'"))
    yield ROLE
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{DB}" WITH (FORCE)'))
        c.execute(text(f'DROP ROLE IF EXISTS "{ROLE}"'))
    admin.dispose()


def app_url() -> str:
    return make_url(ADMIN).set(username=ROLE, password=PASSWORD, database=DB).render_as_string(hide_password=False)


def test_a_createdb_role_creates_owns_and_migrates_the_database(role: str) -> None:
    url = app_url()
    assert ensure_database(url) is True
    assert ensure_database(url) is False  # idempotent
    bootstrap(url)  # migrates
    engine = create_engine(url, poolclass=NullPool)
    try:
        assert current_revision(engine) == head_revision()
        with engine.connect() as c:
            assert c.execute(text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()")).scalar() == ROLE
            c.execute(text("CREATE TABLE _probe(id int)"))  # the owner can create in `public` (PG15+ default is owner-only)
    finally:
        engine.dispose()
    bootstrap(url)  # a second boot is a no-op


def test_two_instances_booting_at_once_do_not_race(role: str) -> None:
    url = app_url()
    errors: list[BaseException] = []

    def boot() -> None:
        try:
            bootstrap(url)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=boot) for _ in range(3)]
    [t.start() for t in threads]
    [t.join(120) for t in threads]
    assert errors == []
    engine = create_engine(url, poolclass=NullPool)
    try:
        assert current_revision(engine) == head_revision()
    finally:
        engine.dispose()
