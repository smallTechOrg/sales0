"""SMA-48: the admin's AI settings page - which provider, endpoint, model and key the app uses, and a Test button.

Saving writes the single ``ai_provider_config`` row (the key Fernet-encrypted, never shown again: an empty key field
keeps the stored one). The Test runs on the values in the form, saved or not, and never changes the app-wide "is the
AI working" state: (1) "Reply with OK" - does the endpoint answer with this key and model; (2) one small forced
function call - the app's AI features all need function calling, so a provider that cannot do it is reported.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.models import AiProviderConfig, TeamMember
from sales0.db.models.ai_provider import AI_PROVIDER_ROW_ID, AI_PROVIDERS
from sales0.errors import DomainError
from sales0.llm import LLMError, StructuredRequest, build_client, reset_llm_client
from sales0.llm.provider import ProviderConfig, from_row, server_config
from sales0.logging import get_logger

log = get_logger("sales0.ai_settings")

# Shown as one-click presets on the page (the admin can type any other OpenAI-compatible URL).
PRESETS: tuple[tuple[str, str], ...] = (
    ("OpenAI", "https://api.openai.com/v1"),
    ("OpenRouter", "https://openrouter.ai/api/v1"),
    ("Groq", "https://api.groq.com/openai/v1"),
    ("Together", "https://api.together.xyz/v1"),
    ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("Ollama (this machine)", "http://localhost:11434/v1"),
)
_BLOCKED_HOSTS = frozenset({"metadata.google.internal", "metadata", "169.254.169.254"})
TEST_TOOL = "report_status"


@dataclass(frozen=True)
class SettingsView:
    provider: str
    base_url: str
    model: str
    research_model: str
    price_in: float
    price_out: float
    key_stored: bool
    key_unreadable: bool
    key_hint: str  # last 4 characters, e.g. "...a1b2"; "" when none
    updated_at: datetime | None
    updated_by: str | None
    server_model: str
    server_key_present: bool


def _row(db: Session) -> AiProviderConfig | None:
    return db.get(AiProviderConfig, AI_PROVIDER_ROW_ID)


def view(db: Session) -> SettingsView:
    row = _row(db)
    s = get_settings()
    cfg = from_row(row) if row is not None and row.api_key_encrypted else None
    key = cfg.api_key if cfg is not None else ""
    who = db.get(TeamMember, row.updated_by_id) if row is not None and row.updated_by_id else None
    return SettingsView(
        provider=row.provider if row is not None else "anthropic",
        base_url=(row.base_url or "") if row is not None else "",
        model=(row.model or "") if row is not None else "",
        research_model=(row.research_model or "") if row is not None else "",
        price_in=float(row.price_input_usd_per_mtok) if row is not None else 0.0,
        price_out=float(row.price_output_usd_per_mtok) if row is not None else 0.0,
        key_stored=bool(row is not None and row.api_key_encrypted),
        key_unreadable=bool(cfg is not None and cfg.key_unreadable),
        key_hint=f"...{key[-4:]}" if len(key) >= 8 else "",
        updated_at=row.updated_at if row is not None else None,
        updated_by=who.name if who is not None else None,
        server_model=s.llm_model,
        server_key_present=s.api_key_present,
    )


def clean_url(raw: str) -> str:
    """A usable base URL (http/https, a host, no credentials), without a trailing ``/chat/completions`` or slash."""
    url = (raw or "").strip()
    if not url:
        raise DomainError("invalid_ai_settings", "Enter the endpoint URL (for example https://api.openai.com/v1).", 422)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise DomainError("invalid_ai_settings", "The endpoint URL must start with https:// (or http:// for a local server).", 422)
    if parsed.username or parsed.password:
        raise DomainError("invalid_ai_settings", "Put the key in the API key field, not in the URL.", 422)
    host = parsed.hostname.lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if host in _BLOCKED_HOSTS or (ip is not None and ip.is_link_local):
        raise DomainError("invalid_ai_settings", "That address cannot be used as an AI endpoint.", 422)
    url = url.rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    return url[:500]


def _price(raw: Any) -> float:
    text = str(raw if raw is not None else "").strip()
    if not text:
        return 0.0
    try:
        value = float(text)
    except ValueError as exc:
        raise DomainError("invalid_ai_settings", "Prices are numbers in USD per million tokens (for example 0.15).", 422) from exc
    if value < 0 or value > 1000:
        raise DomainError("invalid_ai_settings", "Prices are between 0 and 1000 USD per million tokens.", 422)
    return value


def config_from_form(db: Session, form: dict[str, Any]) -> ProviderConfig:
    """The ``ProviderConfig`` the submitted form stands for (an empty key field = the stored key)."""
    provider = str(form.get("provider") or "anthropic").strip()
    if provider not in AI_PROVIDERS:
        raise DomainError("invalid_ai_settings", "Choose Anthropic or OpenAI-compatible.", 422)
    if provider == "anthropic":
        return server_config()
    model = " ".join(str(form.get("model") or "").split())[:200]
    if not model:
        raise DomainError("invalid_ai_settings", "Enter the model name (for example gpt-4o-mini).", 422)
    key = str(form.get("api_key") or "").strip()
    stored = from_row(_row(db)) if not key else None
    unreadable = False
    if stored is not None and stored.provider == "openai":
        key, unreadable = stored.api_key, stored.key_unreadable
    return ProviderConfig(
        provider="openai",
        model=model,
        research_model=" ".join(str(form.get("research_model") or "").split())[:200],
        base_url=clean_url(str(form.get("base_url") or "")),
        api_key=key,
        price_input_usd_per_mtok=_price(form.get("price_in")),
        price_output_usd_per_mtok=_price(form.get("price_out")),
        source="admin",
        key_unreadable=unreadable,
    )


def save(db: Session, form: dict[str, Any], admin: TeamMember) -> ProviderConfig:
    """Validate and store the form; the new provider is used straight away (other instances within 30 s)."""
    cfg = config_from_form(db, form)
    row = _row(db)
    if row is None:
        row = AiProviderConfig(id=AI_PROVIDER_ROW_ID)
        db.add(row)
    row.provider = cfg.provider
    row.updated_at = datetime.now(UTC)
    row.updated_by_id = admin.id
    if cfg.provider == "openai":
        from sales0.services.team import crypto

        row.base_url, row.model, row.research_model = cfg.base_url, cfg.model, cfg.research_model or None
        row.price_input_usd_per_mtok, row.price_output_usd_per_mtok = cfg.price_input_usd_per_mtok, cfg.price_output_usd_per_mtok
        new_key = str(form.get("api_key") or "").strip()
        if new_key:
            row.api_key_encrypted = crypto.encrypt(new_key)
        elif str(form.get("clear_key") or "") == "1":
            row.api_key_encrypted = None
            cfg = replace(cfg, api_key="", key_unreadable=False)
    db.flush()
    reset_llm_client()
    log.info("ai_provider_saved", provider=cfg.provider, model=cfg.model, host=urlparse(cfg.base_url).hostname, by=admin.id)
    return cfg


@dataclass(frozen=True)
class TestStep:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class TestResult:
    ok: bool
    provider: str
    model: str
    steps: tuple[TestStep, ...]

    @property
    def summary(self) -> str:
        if self.ok:
            return f"Working: {self.provider} answered with model {self.model} and can fill in the app's forms."
        failed = next((s for s in self.steps if not s.ok), None)
        return f"Not working: {failed.detail}" if failed else "Not working."


def _test_request() -> StructuredRequest:
    return StructuredRequest(
        feature="ping",
        system="You are a connectivity check. Call the function exactly once.",
        messages=[{"role": "user", "content": "Report status ok=true and the word 'ready'."}],
        tool_name=TEST_TOOL,
        tool_description="Report that the connection works.",
        input_schema={"type": "object", "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}},
                      "required": ["ok", "word"]},  # fmt: skip
        max_tokens=60,
        prompt_version="ai-settings-test-v1",
    )


def run_test(cfg: ProviderConfig, *, client: Any = None) -> TestResult:
    """Two tiny calls on ``cfg`` (nothing is stored; the app's AI status is not touched)."""
    label, model = cfg.label, cfg.model or get_settings().llm_model
    steps: list[TestStep] = []
    try:
        llm = client or build_client(cfg, record_health=False)
    except LLMError as exc:
        return TestResult(False, label, model, (TestStep("Connect", False, exc.message),))
    try:
        resp = llm.ping()
        said = (resp.text or "").strip()[:40] or "(no text)"
        steps.append(TestStep("Reply", True, f"Answered \"{said}\" in {resp.latency_ms} ms ({resp.model})."))
    except LLMError as exc:
        steps.append(TestStep("Reply", False, exc.message))
        return TestResult(False, label, model, tuple(steps))
    try:
        resp = llm.structured(_test_request())
        if isinstance(resp.tool_input, dict) and "ok" in resp.tool_input:
            steps.append(TestStep("Function calling", True, f"Filled in a test form in {resp.latency_ms} ms."))
        else:
            steps.append(TestStep("Function calling", False, "The model answered but did not fill in the test form "
                                  "(function / tool calling). Triage, research and drafting need it - choose a model that supports tools."))  # fmt: skip
    except LLMError as exc:
        steps.append(TestStep("Function calling", False, exc.message))
    return TestResult(all(s.ok for s in steps), label, model, tuple(steps))


__all__ = ["PRESETS", "SettingsView", "TestResult", "TestStep", "clean_url", "config_from_form", "run_test", "save", "view"]
