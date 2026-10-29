"""Where the Phase-2 integrator registers the real (SQLAlchemy) repository.

The CLI and the app lifespan call :func:`get_repository`; with nothing registered the CLI
falls back to an in-memory repository (counts only, nothing persisted).
"""

from __future__ import annotations

from collections.abc import Callable

from sales0.services.sheetsync.repository import SheetRepository

_factory: Callable[[], SheetRepository] | None = None


def register_repository_factory(factory: Callable[[], SheetRepository] | None) -> None:
    global _factory
    _factory = factory


def get_repository() -> SheetRepository | None:
    return _factory() if _factory is not None else None


def repository_installed() -> bool:
    """True once a real (CRM) repository factory is registered."""
    return _factory is not None
