"""Token cost in integer micro-USD.

Prices are USD per 1M tokens and 1 micro-USD = 1e-6 USD, so tokens x $/MTok is already
micro-USD: cost_usd_micros = round(input_tokens * price_in + output_tokens * price_out).
"""

from __future__ import annotations

from sales0.config import Settings


# Prompt caching (5-minute ephemeral TTL): a cache write costs 1.25x the input price,
# a cache read 0.1x. ``input_tokens`` is the API's uncached remainder (cache tokens are
# reported separately by Anthropic and are NOT included in it).
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1


def prices_for(model: str | None, settings: Settings) -> tuple[float, float]:
    """(input, output) USD per 1M tokens: the research and keyword-ideas models have their own price, every other model
    the default one."""
    if model and model != settings.llm_model:
        if model == settings.research_model:
            return settings.research_price_input_usd_per_mtok, settings.research_price_output_usd_per_mtok
        if model == settings.keyword_ideas_model:
            return settings.keyword_ideas_price_input_usd_per_mtok, settings.keyword_ideas_price_output_usd_per_mtok
    return settings.llm_price_input_usd_per_mtok, settings.llm_price_output_usd_per_mtok


def cost_usd_micros(
    input_tokens: int,
    output_tokens: int,
    settings: Settings,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    model: str | None = None,
) -> int:
    if min(input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens) < 0:
        raise ValueError("token counts must be non-negative")
    price_in, price_out = prices_for(model, settings)
    return round(
        input_tokens * price_in
        + cache_creation_input_tokens * price_in * CACHE_WRITE_MULTIPLIER
        + cache_read_input_tokens * price_in * CACHE_READ_MULTIPLIER
        + output_tokens * price_out
    )


def micros_to_usd(micros: int) -> float:
    return micros / 1_000_000


def web_search_cost_usd_micros(searches: int, settings: Settings) -> int:
    """Server-side web searches are billed per search (USD per 1,000): $10 / 1k = 10_000 micro-USD each."""
    if searches < 0:
        raise ValueError("searches must be non-negative")
    return round(searches * settings.llm_price_web_search_usd_per_1k * 1000)
