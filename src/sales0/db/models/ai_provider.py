"""SMA-48: the AI provider an admin chose on the AI settings page (one row, id 1).

No row (or provider 'anthropic') = the server's own Anthropic settings (SALES0_ANTHROPIC_API_KEY, SALES0_LLM_MODEL).
'openai' = any OpenAI-compatible Chat Completions endpoint (OpenAI, OpenRouter, Groq, Together, a local Ollama /
vLLM / LM Studio ...). The key is stored Fernet-encrypted (``SALES0_SECRET_KEY``) and never shown again."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc

AI_PROVIDERS = ("anthropic", "openai")
AI_PROVIDER_ROW_ID = 1


class AiProviderConfig(Base):
    __tablename__ = "ai_provider_config"
    __table_args__ = (in_check("provider", AI_PROVIDERS, "provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="anthropic", server_default="anthropic")
    base_url: Mapped[str | None] = mapped_column(String(500))
    model: Mapped[str | None] = mapped_column(String(200))
    # Used where the app asks for its stronger model (research, keyword ideas, summaries). Empty = ``model``.
    research_model: Mapped[str | None] = mapped_column(String(200))
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    price_input_usd_per_mtok: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    price_output_usd_per_mtok: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"))
