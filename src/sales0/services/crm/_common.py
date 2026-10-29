"""Small helpers shared by the CRM services."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sales0.clock import local_today
from sales0.config import get_settings
from sales0.errors import DomainError


class _Unset:
    """Sentinel: "argument not supplied" (distinct from None, which clears a value)."""

    _instance: _Unset | None = None

    def __new__(cls) -> _Unset:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Any = _Unset()


def page_params(page: int, page_size: int | None) -> tuple[int, int]:
    size = page_size or get_settings().crm_page_size
    return max(int(page or 1), 1), max(min(int(size), 500), 1)


def today_local(now: datetime | None = None) -> date:
    return local_today(get_settings().tz, now)


def invalid(code: str, message: str, status: int = 400) -> DomainError:
    return DomainError(code, message, status)
