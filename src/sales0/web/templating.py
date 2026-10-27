"""Jinja2 environment shared by every page, with the project's display filters."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path

from fastapi.templating import Jinja2Templates

from sales0.clock import ensure_utc, utcnow
from sales0.config import get_settings

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

_VERDICT_LABELS = {"genuine": "Genuine", "discard": "Discard"}


def usd(micros: int | None) -> str:
    """Integer micro-USD → display string; at least 4 decimals below $0.01 (e.g. $0.0012)."""
    if micros is None:
        return "—"
    value = micros / 1_000_000
    if micros == 0:
        return "$0.00"
    if abs(value) < 0.01:
        return f"${value:.4f}"
    return f"${value:,.2f}"


def tokens(count: int | None) -> str:
    if count is None:
        return "—"
    return f"{count:,}"


def localdt(value: datetime | None, fmt: str = "%d %b %Y, %H:%M") -> str:
    if value is None:
        return "—"
    return ensure_utc(value).astimezone(get_settings().tz).strftime(fmt)


def ago(value: datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return "never"
    seconds = int(((now or utcnow()) - ensure_utc(value)).total_seconds())
    if seconds < 0:
        return localdt(value)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 86400 * 30:
        return f"{seconds // 86400}d ago"
    return localdt(value, "%d %b %Y")


def verdict_label(verdict: str | None) -> str:
    if verdict is None:
        return "Not triaged"
    return _VERDICT_LABELS.get(verdict, verdict.replace("_", " ").capitalize())


@lru_cache(maxsize=1)
def get_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters.update(
        usd=usd, tokens=tokens, localdt=localdt, ago=ago, verdict_label=verdict_label
    )
    return templates
