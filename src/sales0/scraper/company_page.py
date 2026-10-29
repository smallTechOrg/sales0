"""Read ONE LinkedIn company page (about + recent posts, as visible text) with an OPEN ``LinkedInSearcher``.

Used by the laptop worker for prospect research (``worker_tasks`` kind ``company_page``). Same account safety as a
search pass: a checkpoint before the navigation, after it and after the one gentle scroll (latched 429 / checkpoint
/ captcha / logout -> ``AccountSafetyStop``), a paced random delay before every page action, the page load counted
against the searcher's budget. It never clicks anything and never opens a second page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from sales0.logging import get_logger
from sales0.scraper.errors import ScrapeError

log = get_logger("sales0.scraper.company_page")

MAX_TEXT_CHARS = 20_000
PAGE_TEXT_JS = """() => {
  const root = document.querySelector('main') || document.body;
  return {title: document.title || '', text: (root && root.innerText) || ''};
}"""


@dataclass(frozen=True)
class CompanyPageText:
    title: str
    url: str
    text: str


def company_page_url(base: str, slug: str) -> str:
    """``company/<x>`` -> ``<base>/company/<x>/``. ValueError for anything else (never an arbitrary URL)."""
    kind, _, name = slug.partition("/")
    if kind != "company" or not name or "/" in name or not re.fullmatch(r"[A-Za-z0-9\-_.~%]+", name):
        raise ValueError(f"Not a company slug: {slug!r}")
    return f"{base}/company/{quote(name, safe='-_.~%')}/"


def clean_text(raw: str, limit: int = MAX_TEXT_CHARS) -> str:
    lines = [" ".join(line.split()) for line in (raw or "").splitlines()]
    out: list[str] = []
    for line in lines:
        if not line or (out and out[-1] == line):
            continue
        out.append(line)
    text = "\n".join(out)
    return text[:limit]


def read_company_page(searcher: Any, slug: str, max_chars: int = MAX_TEXT_CHARS) -> CompanyPageText:
    """Visit ``company/<x>`` once and return its visible text. Raises ``AccountSafetyStop`` / ``ScrapeError``."""
    from playwright.sync_api import Error as PlaywrightError

    page = getattr(searcher, "_page", None)
    if page is None or not searcher.is_open:
        raise ScrapeError("LinkedInSearcher is not open (use it as a context manager)")
    if searcher._budget_exhausted():
        raise ScrapeError("The daily LinkedIn page-load budget is used up")
    url = company_page_url(searcher._base, slug)
    what = f" for {slug}"
    searcher._checkpoint(page, what=what)
    searcher._pacer.delay()
    searcher._page_loads += 1  # counted when attempted
    try:
        response = page.goto(url, wait_until="domcontentloaded")
    except PlaywrightError as exc:
        searcher._raise_if_latched()
        raise ScrapeError(f"Could not open the company page{what}: {exc}") from exc
    searcher._checkpoint(page, response, what=what)
    searcher._pacer.delay()
    searcher._wheel(page, searcher._pacer.randint(500, 1000))  # one gentle scroll: the recent posts render lazily
    page.wait_for_timeout(searcher._pacer.jitter_ms(800))
    searcher._checkpoint(page, what=what)
    try:
        data = page.evaluate(PAGE_TEXT_JS)
    except PlaywrightError as exc:
        searcher._raise_if_latched()
        raise ScrapeError(f"Could not read the company page{what}: {exc}") from exc
    text = clean_text(str((data or {}).get("text") or ""), max_chars)
    if not text:
        raise ScrapeError(f"The company page{what} had no readable text")
    log.info("company_page_read", slug=slug, chars=len(text))
    return CompanyPageText(title=str((data or {}).get("title") or "")[:300], url=page.url[:1000], text=text)


__all__ = ["CompanyPageText", "clean_text", "company_page_url", "read_company_page"]
