"""The fixture LinkedIn corpus as triage inputs (architecture.md -> Fixture LinkedIn corpus).

``CORPUS`` is loaded from ``src/sales0/linkedin_fixture/posts.json`` — the exact text the
discovery run scrapes and feeds to the agent — keyed by post id. Only the spec's P1–P11 are
taken (P7, the content duplicate of P2, is merged and never triaged); any other entry
(e.g. safety-trigger keywords added for scraper tests) is ignored. Extra edge cases live as
explicit inputs in ``test_triage_edges_and_errors.py``.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import sales0.linkedin_fixture
from sales0.agent.schemas import OfferingRef, TriageInput

# data.md -> offerings seed rows (exact)
SEED_OFFERINGS = [
    OfferingRef(
        id=1,
        name="AI corporate workshops",
        group="Training",
        description="Instructor-led AI/GenAI awareness and productivity workshops for company teams, managers and "
        "leadership, online or on-site.",
    ),
    OfferingRef(
        id=2,
        name="Technical AI training",
        group="Training",
        description='Hands-on technical programs for engineers, e.g. "Build your first AI agent", Claude Code, '
        "LLM app development.",
    ),
    OfferingRef(
        id=3,
        name="AI consulting & build services",
        group="Services",
        description="Consulting, design and build of AI solutions: assistants/chatbots, agents, automation, RAG over "
        "company documents.",
    ),
    OfferingRef(id=4, name="Products", group="Products", description="smallTech's AI products and tools offered to companies."),
]
OFFERING_ID = {o.name: o.id for o in SEED_OFFERINGS}

POSTS_JSON = Path(sales0.linkedin_fixture.__file__).with_name("posts.json")
TRIAGED_IDS = ("P1", "P2", "P3", "P5", "P6", "P8", "P9", "P10", "P11")  # spec table minus P7
POSTED = date(2026, 10, 26)
_ID = re.compile(r"^P(\d+)$")


def _permalink(urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{urn}/"


def load_corpus() -> dict[str, TriageInput]:
    by_id = {p["id"]: p for p in json.loads(POSTS_JSON.read_text(encoding="utf-8")) if isinstance(p, dict)}
    missing = [pid for pid in TRIAGED_IDS if pid not in by_id]
    assert not missing, f"posts.json is missing spec corpus posts {missing}"
    corpus: dict[str, TriageInput] = {}
    for pid in TRIAGED_IDS:
        post = by_id[pid]
        corpus[pid] = TriageInput(
            enquiry_id=int(_ID.match(pid).group(1)),  # type: ignore[union-attr]
            author_name=post["author_name"],
            author_headline=post["author_headline"],
            source_url=_permalink(post["urn"]),
            date_posted=POSTED,
            text=post["text"],
        )
    return corpus


CORPUS: dict[str, TriageInput] = load_corpus()
