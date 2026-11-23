"""q28 (SMA-48): ``ai_provider_config`` - the AI provider an admin chose (Anthropic from the server settings, or any
OpenAI-compatible endpoint with its own URL, model and encrypted key). One row at most; no row = Anthropic.

Revision ID: q28_ai_provider_8d2f6b14
Revises: q27_stages_paths_4c8e1a52
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q28_ai_provider_8d2f6b14"
down_revision: str | Sequence[str] | None = "q27_stages_paths_4c8e1a52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_provider_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False, server_default="anthropic"),
        sa.Column("base_url", sa.String(500), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("research_model", sa.String(200), nullable=True),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("price_input_usd_per_mtok", sa.Float(), nullable=False, server_default="0"),
        sa.Column("price_output_usd_per_mtok", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), sa.ForeignKey("team_members.id", ondelete="SET NULL"), nullable=True),
        sa.CheckConstraint("provider IN ('anthropic', 'openai')", name="ck_ai_provider_config_provider"),
    )


def downgrade() -> None:
    op.drop_table("ai_provider_config")
