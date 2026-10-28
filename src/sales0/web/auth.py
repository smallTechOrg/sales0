"""Shared-password login (S3 replaces this file wholesale)."""

from __future__ import annotations

from fastapi import FastAPI

from sales0.config import Settings


def install(app: FastAPI, settings: Settings) -> None:
    """Add the auth middleware and the login router. No-op until S3."""
