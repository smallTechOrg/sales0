"""The 60 buyer-intent phrases Sales0 suggests as keywords (capabilities/search-setup.md, Phase 1.4).

Pure data, no imports from the rest of the app: the seeding service, the fixture LinkedIn and the
scraper tests all import ``SUGGESTED_KEYWORDS``. ``key`` is the stable slug of the text (the
``keywords.suggested_key`` seeding identity); ``group`` is an offering name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def suggested_key_of(text: str) -> str:
    """Lower-case, runs of non-alphanumerics -> ``-``, trimmed, at most 60 chars."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-")


@dataclass(frozen=True)
class SuggestedKeyword:
    key: str
    text: str
    group: str


def _group(group: str, *texts: str) -> tuple[SuggestedKeyword, ...]:
    return tuple(
        SuggestedKeyword(key=suggested_key_of(t), text=t, group=group) for t in texts
    )


_G1 = _group(
    "AI corporate workshops",
    "looking for AI trainer",
    "corporate AI training",
    "urgent trainer requirement AI",
    "GenAI workshop for employees",
    "AI enablement workshop",
    "AI training partner",
    "AI upskilling program",
    "AI corporate workshop",
    "AI training for managers",
    "AI workshop for leadership team",
    "ChatGPT training for our team",
    "corporate generative AI training",
    "AI awareness session for employees",
    "AI literacy workshop for teams",
    "looking for AI training vendor",
)

_G2 = _group(
    "Technical AI training",
    "Claude Code trainer",
    "agentic AI workshop",
    "build AI agents training for developers",
    "LLM training for engineers",
    "prompt engineering workshop for company",
    "Claude Code training for developers",
    "AI agent workshop for engineering team",
    "RAG workshop for developers",
    "hands-on GenAI bootcamp for engineers",
    "AI coding assistant training",
    "LangChain training for team",
    "LLM application development training",
    "AI upskilling for software engineers",
    "agentic AI training for engineers",
    "GenAI hackathon workshop for tech team",
)

_G3 = _group(
    "AI consulting & build services",
    "hiring AI consultant",
    "build AI agent for our company",
    "AI automation partner",
    "need chatbot developer company",
    "looking for AI development company",
    "RAG chatbot over company documents",
    "AI automation agency for business",
    "GenAI implementation partner",
    "AI solution for customer support",
    "AI consulting firm India",
    "enterprise AI assistant development",
    "AI proof of concept partner",
    "AI strategy consultant for company",
    "need AI agent development vendor",
    "custom AI chatbot for our website",
)

_G4 = _group(
    "Products",
    "AI tool for our sales team",
    "AI meeting notes tool for company",
    "AI knowledge base software for company",
    "AI document search tool for enterprise",
    "AI chatbot platform for business",
    "AI customer support software",
    "AI workflow automation platform",
    "AI copilot for our employees",
    "GenAI platform vendor for enterprise",
    "AI tool for HR team",
    "AI lead generation tool",
    "private LLM for our company",
    "AI content generation tool for marketing team",
    "AI analytics assistant for business",
    "AI voice agent for our business",
)

GROUP_NAMES: tuple[str, ...] = (
    "AI corporate workshops",
    "Technical AI training",
    "AI consulting & build services",
    "Products",
)
SUGGESTED_KEYWORDS: tuple[SuggestedKeyword, ...] = _G1 + _G2 + _G3 + _G4
