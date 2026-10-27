"""Token cost in integer micro-USD.

Prices are USD per 1M tokens and 1 micro-USD = 1e-6 USD, so tokens x $/MTok is already
micro-USD: cost_usd_micros = round(input_tokens * price_in + output_tokens * price_out).
"""

from __future__ import annotations

from sales0.config import Settings


def cost_usd_micros(input_tokens: int, output_tokens: int, settings: Settings) -> int:
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts must be non-negative")
    return round(
        input_tokens * settings.llm_price_input_usd_per_mtok
        + output_tokens * settings.llm_price_output_usd_per_mtok
    )


def micros_to_usd(micros: int) -> float:
    return micros / 1_000_000
