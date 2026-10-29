"""Triage criteria: the include / exclude rules rendered into the triage system prompt.

The fixed scaffolding (company description, priority rules, output fields, safety, built-in examples) stays in
``prompts/triage_system.md``; the two rule lists come from the ``triage_criteria`` table (the team edits them on the
Criteria page). ``DEFAULT_RULES`` is the seed: the rules that were hard-coded in the prompt up to triage-v3, verbatim,
so a fresh database renders the very same system prompt. ``migrations/versions/q14_criteria_5b8e1f3a.py`` carries a
FROZEN copy; ``tests/unit/agent/test_criteria_prompt.py`` asserts they stay equal.

Pure: no DB access here (``services.criteria`` loads the active set).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

CriterionKind = Literal["include", "exclude"]

DEFAULT_INCLUDE: tuple[str, ...] = (
    'A company, institute, HR / L&D, CTO / engineering or business person (or someone writing for one) is SEEKING a '
    'training / workshop / consulting / build PARTNER or an AI product: "looking for a training partner / vendor / '
    'company", "need an AI workshop for our team / employees", "looking for an AI trainer for my company / team", '
    '"want an assistant built". -> `opportunity_type=direct_buyer`.',
    "An agency, edtech or training company, recruiter or staffing firm posts a CONCRETE requirement (a named client or "
    "programme, topic, dates, city, batch size or duration) and invites trainers, SMEs, training providers, academies, "
    "organisations or partners to respond with a profile, TOC / curriculum, proposal or commercials. Even if it says "
    '"Trainer requirement", "URGENT", "for our client" or "share your profile and rates", this is a business '
    "opportunity the team routinely pursues. -> `opportunity_type=agency_requirement`. Priority `medium`; `low` when "
    "thin (no dates, no location, vague topic, no way to respond).",
    'An RFP, tender, vendor-empanelment call or "training providers register here" / provider-pool form from a company '
    "or institute. -> `agency_requirement` (or `direct_buyer` when the company is itself the buyer).",
    "A concrete requirement for one or a few freelance / contract trainers to deliver a corporate or institutional "
    "programme (named topic, audience, dates or location), where the poster is NOT clearly only after a cheap hourly "
    "tutor. -> `opportunity_type=individual_trainer_wanted`, priority `low` (`medium` if a company is named and the "
    "audience is a team).",
    "Posts in Hindi, Hinglish or heavy with emoji are judged the same way.",
)

DEFAULT_EXCLUDE: tuple[str, ...] = (
    'A full-time / permanent / on-payroll EMPLOYEE job post (CTC, LPA, salary, "apply with your resume", "immediate '
    'joiners"), including "AI Trainer" or "Corporate Trainer" roles on payroll. (A part-time / contract / freelance '
    'engagement to deliver training or upskill a team is NOT this case, even when worded "We\'re hiring an AI '
    'trainer": that is `genuine`, `individual_trainer_wanted`.)',
    'An individual shopping for ONE cheap tutor: hourly or per-hour rates, tiny personal batch, "weekend batch, DM '
    'your rates", no organisation behind it. -> `individual_trainer_wanted`.',
    'A trainer, consultant or training company ADVERTISES itself, is "open to work", offers batches / services, or '
    "promotes a partner programme. -> `poster_type=job_seeker_or_self_promo`.",
    'Event, webinar, bootcamp, cohort or course promotion (including "register now", "limited seats", discount '
    "codes, counselling calls).",
    "Thought leadership, news, opinion, celebration, a listicle, a product launch or a company hiring announcement for "
    "non-training roles.",
    "A recruiter or agency growing its trainer NETWORK with no concrete requirement at all and no programme, client, "
    'topic list or schedule ("onboarding freelance trainers", "register on our portal for future assignments", payout '
    "per session, no client, topic dates or city).",
    "The request is clearly outside smallTech's offerings (e.g. SAP ABAP, Tally, non-AI, non-technology topics only).",
    "Spam, pyramid / MLM schemes, or irrelevant content.",
)

MAX_RULE_CHARS = 600


@dataclass(frozen=True)
class Criterion:
    kind: CriterionKind
    text: str


DEFAULT_RULES: tuple[Criterion, ...] = tuple(Criterion("include", t) for t in DEFAULT_INCLUDE) + tuple(
    Criterion("exclude", t) for t in DEFAULT_EXCLUDE
)


@dataclass(frozen=True)
class CriteriaSet:
    """The ACTIVE rules in display order. ``version`` identifies the rule set in ``triage_criteria_sets``
    (None = the built-in defaults, e.g. a database without the criteria tables, or the eval)."""

    rules: tuple[Criterion, ...] = field(default=DEFAULT_RULES)
    version: int | None = None

    @property
    def include(self) -> list[str]:
        return [r.text for r in self.rules if r.kind == "include"]

    @property
    def exclude(self) -> list[str]:
        return [r.text for r in self.rules if r.kind == "exclude"]


DEFAULT_SET = CriteriaSet()

__all__ = ["DEFAULT_EXCLUDE", "DEFAULT_INCLUDE", "DEFAULT_RULES", "DEFAULT_SET", "MAX_RULE_CHARS", "CriteriaSet", "Criterion"]
