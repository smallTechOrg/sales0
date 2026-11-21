"""Is the AI (Anthropic) working right now? Shown as a banner on every page, on the Health page and next to the forms
that use it, so a person knows to fill things in by hand.

Down when: no API key; the monthly spend cap is used up; or the latest AI call failed with no success since. The
reason comes from the provider's own error when this process saw it (e.g. "credit balance is too low"), else from the
stored ``llm_calls`` rows (other instances / after a restart). A one-off bad answer does not count: an unexpected
response only marks the AI down when the last two calls both failed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc
from sales0.config import get_settings
from sales0.db.models import LlmCall
from sales0.llm import health
from sales0.logging import get_logger

log = get_logger("sales0.ai_status")

FATAL = ("auth", "workspace_required", "model_not_found", "transient")
WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class AiStatus:
    down: bool
    label: str = "AI working"
    reason: str = ""
    fix: str = ""
    since: datetime | None = None


def _explain(error_type: str, message: str) -> tuple[str, str]:
    text = (message or "").lower()
    if "credit balance" in text or "billing" in text:
        return "the Anthropic account is out of credits", "Top up the Anthropic account (console.anthropic.com, Plans & Billing)."
    if error_type in ("auth", "workspace_required"):
        return "Anthropic rejected the API key", "Check SALES0_ANTHROPIC_API_KEY (and the workspace id) on the server."
    if error_type == "model_not_found":
        return "the AI model is not available on this account", "Check SALES0_LLM_MODEL / SALES0_RESEARCH_MODEL."
    if error_type == "transient":
        return "Anthropic is not responding (overloaded or unreachable)", "Usually passes by itself - try again in a few minutes."
    return "AI calls are failing", "See the Health page; the server log has the provider's message."


def status(db: Session, now: datetime | None = None) -> AiStatus:
    try:
        return _status(db, now or datetime.now(UTC))
    except Exception as exc:  # noqa: BLE001 - the status line must never break a page
        log.warning("ai_status_failed", error=f"{type(exc).__name__}: {exc}"[:200])
        return AiStatus(False)


def _status(db: Session, now: datetime) -> AiStatus:
    settings = get_settings()
    if not settings.api_key_present:
        return AiStatus(True, "AI unavailable", "no API key is set", "Set SALES0_ANTHROPIC_API_KEY on the server.")
    from sales0.services import costs_report

    cap = costs_report.spend_cap_status(db)
    if cap.state == "exceeded":
        return AiStatus(True, "AI paused", "this month's AI spend cap is used up", "Raise SALES0_MONTHLY_SPEND_CAP_USD or wait for next month.")
    failure, ok_at = health.snapshot()
    if failure is not None and (ok_at is None or failure.at > ok_at) and now - failure.at < WINDOW:
        if failure.error_type in FATAL or "credit balance" in failure.message.lower():
            reason, fix = _explain(failure.error_type, failure.message)
            return AiStatus(True, "AI unavailable", reason, fix, failure.at)
    since = now - WINDOW
    last_ok = db.scalar(select(func.max(LlmCall.created_at)).where(LlmCall.status == "ok", LlmCall.created_at >= since))
    recent = list(db.scalars(select(LlmCall).where(LlmCall.created_at >= since).order_by(LlmCall.created_at.desc(), LlmCall.id.desc()).limit(2)))
    if not recent or recent[0].status != "error":
        return AiStatus(False)
    latest = recent[0]
    etype = latest.error_type or ""
    both_failed = len(recent) == 2 and recent[1].status == "error"
    if etype in FATAL or both_failed or (failure is not None and "credit balance" in failure.message.lower()):
        if last_ok is not None and ensure_utc(last_ok) > ensure_utc(latest.created_at):
            return AiStatus(False)
        reason, fix = _explain(etype, failure.message if failure is not None and failure.error_type == etype else "")
        return AiStatus(True, "AI unavailable", reason, fix, ensure_utc(latest.created_at))
    return AiStatus(False)


@dataclass(frozen=True)
class Missed:
    """Work the AI did not do (SMA-39): shown in the AI banner and on Health, with where to finish it."""
    count: int
    text: str
    href: str


def missed(db: Session) -> list[Missed]:
    """What is waiting because an AI call failed: enquiries it could not read, found posts it could not check."""
    try:
        from sales0.db.models import Enquiry, SeenPost

        out: list[Missed] = []
        n = int(db.scalar(select(func.count()).select_from(Enquiry).where(Enquiry.triage_status == "failed",
                                                                          Enquiry.review_status != "discarded")) or 0)
        if n:
            out.append(Missed(n, f"{n} enquir{'y' if n == 1 else 'ies'} not read by the AI (summary, company, contact not "
                                 "filled in) - fill them in by hand or press Retry triage", "/inbox?tab=review"))
        k = int(db.scalar(select(func.count()).select_from(SeenPost).where(SeenPost.triage_status == "failed",
                                                                         SeenPost.enquiry_id.is_(None))) or 0)
        if k:
            out.append(Missed(k, f"{k} found post{'' if k == 1 else 's'} not checked by the AI", "/inbox?tab=skipped"))
        return out
    except Exception as exc:  # noqa: BLE001 - never break a page
        log.warning("ai_missed_failed", error=f"{type(exc).__name__}: {exc}"[:200])
        return []


def check_now(db: Session, llm=None) -> AiStatus:  # type: ignore[no-untyped-def]
    """One tiny real call (\"Reply with OK\") to see whether the AI answers; recorded like any other call."""
    from sales0.llm import LLMError, get_llm_client

    stamp = datetime.now(UTC)
    settings = get_settings()
    try:
        resp = (llm or get_llm_client()).ping()
    except LLMError as exc:
        db.add(LlmCall(feature="ping", model=settings.llm_model[:100], prompt_version="ping-v1", status="error",
                       error_type=str(getattr(exc, "error_type", type(exc).__name__))[:100], created_at=stamp))
        db.flush()
        health.note_failure(str(getattr(exc, "error_type", "llm_error")), getattr(exc, "message", str(exc)), "ping")
        return status(db)
    db.add(LlmCall(feature="ping", model=(resp.model or settings.llm_model)[:100], prompt_version="ping-v1", status="ok",
                   input_tokens=resp.input_tokens, output_tokens=resp.output_tokens, cost_usd_micros=resp.cost_usd_micros,
                   latency_ms=resp.latency_ms, created_at=stamp))
    db.flush()
    health.note_success()
    return status(db)


__all__ = ["AiStatus", "Missed", "check_now", "missed", "status"]
