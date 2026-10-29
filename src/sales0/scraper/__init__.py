"""LinkedIn scraper (slice C). Playwright on a persistent profile; returns plain dataclasses
and never touches the DB. The same code drives live LinkedIn and the fixture corpus."""

from sales0.scraper.errors import AccountSafetyStop, ScrapeError
from sales0.scraper.types import ConnectResult, KeywordScrapeResult, ScrapedPost, SessionCheckResult

__all__ = [
    "AccountSafetyStop",
    "ConnectResult",
    "KeywordScrapeResult",
    "ScrapeError",
    "ScrapedPost",
    "SessionCheckResult",
]
