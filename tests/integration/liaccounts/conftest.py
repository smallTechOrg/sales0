"""Fixtures for the LinkedIn-accounts tests: the fixture LinkedIn server + settings from the scraper
tests (never real linkedin.com)."""

from __future__ import annotations

from tests.integration.scraper.conftest import _fast_fixture_waits, fixture_linkedin_server, fixture_settings  # noqa: F401
from tests.integration.web.conftest import client  # noqa: F401  (TestClient on a migrated DB, for the Setup tests)
