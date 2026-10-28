"""Readable text + title + links from an HTML page (stdlib parser; scripts/styles dropped)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

_SKIP = frozenset(
    {"script", "style", "noscript", "template", "svg", "head", "iframe", "object"}
)
_BLOCK = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
     "header", "footer", "tr", "table", "nav", "main", "aside", "blockquote", "pre", "dt", "dd"}
)  # fmt: skip


@dataclass
class PageText:
    title: str = ""
    text: str = ""
    links: list[str] = field(default_factory=list)


class _Extractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._skip_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.parts: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag in _SKIP:
            self._skip_depth += 1
            return
        if tag in _BLOCK:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.lower().startswith(
                ("javascript:", "mailto:", "tel:", "data:")
            ):
                self.links.append(href)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK:
            self.parts.append("\n")
        if tag == "a":
            return

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in _SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif self._skip_depth == 0:
            self.parts.append(data)


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v ]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


def extract_html(html: str, base_url: str) -> PageText:
    parser = _Extractor(base_url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup: keep what was parsed
        pass
    seen: set[str] = set()
    links: list[str] = []
    for href in parser.links:
        absolute = urldefrag(urljoin(base_url, href.strip()))[0]
        if urlsplit(absolute).scheme in ("http", "https") and absolute not in seen:
            seen.add(absolute)
            links.append(absolute)
    return PageText(
        title=_tidy(" ".join(parser.title_parts)),
        text=_tidy("".join(parser.parts)),
        links=links,
    )


def extract_plain(text: str) -> PageText:
    return PageText(title="", text=_tidy(text), links=[])
