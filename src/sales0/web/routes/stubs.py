"""Scaffold placeholder: GET / renders home.html. Slice E replaces this with the
303 redirect to /inbox plus the "Coming in Phase N" stub pages."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from sales0.web.templating import get_templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return get_templates().TemplateResponse(request, "home.html", {})
