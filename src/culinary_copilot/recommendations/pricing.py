"""Estimated recommendation cost from the versioned model registry.

Prices come only from ``culinary_copilot/llm/models.py``, keyed by model
name (Standard tier, synchronous Responses; separate from ingestion Batch
estimates). There is no model-independent override, so switching models
can never apply another model's prices.

Usage semantics (Responses API): ``output_tokens`` already includes
reasoning tokens (``output_tokens_details.reasoning_tokens`` is a
breakdown), so reasoning is never added again.

Limits of the estimate: usage records do not capture cached-input or
cache-write token counts, so every input token is priced at the list
input rate. The cached-input discount and the cache-write surcharge are
not applied. Cost is estimated only from known usage; unknown usage or an
unsupported model gives None, never 0.
"""

from __future__ import annotations

from culinary_copilot.llm.models import PRICING_VERSION, model_spec


def app_price_for(model: str | None) -> tuple[float | None, float | None, str]:
    """Return ``(input, output)`` USD per 1M tokens and the pricing version."""
    spec = model_spec(model)
    if spec is None:
        return None, None, PRICING_VERSION
    return spec.prices.input_per_1m, spec.prices.output_per_1m, PRICING_VERSION


def estimate_cost_usd(
    input_tokens: int | None, output_tokens: int | None, model: str | None
) -> float | None:
    """Estimated cost, or None when usage or pricing is unknown.

    ``output_tokens`` is the provider's total output count, which already
    includes reasoning tokens.
    """
    if input_tokens is None or output_tokens is None:
        return None
    input_p, output_p, _ = app_price_for(model)
    if input_p is None or output_p is None:
        return None
    return input_tokens / 1e6 * input_p + output_tokens / 1e6 * output_p
