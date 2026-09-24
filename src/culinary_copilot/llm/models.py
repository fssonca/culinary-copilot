"""Supported OpenAI models: one registry for configuration and pricing.

Every model setting (``OPENAI_MODEL``, ``LLM_EXTRACTION_MODEL``,
``LLM_APP_MODEL``, ``LLM_REC_MODEL``) must name a model in
``SUPPORTED_MODELS``; configuration refuses anything else. The project
currently uses ``gpt-6-luna`` only. To support another model, add one
``ModelSpec`` here with its documented reasoning-effort values and its
verified Standard-tier prices; nothing else hard-codes a model.

Verified 2026-09-24 against the official pages:
- https://developers.openai.com/api/docs/models/gpt-6-luna: Responses API,
  Batch, structured outputs, function calling (Responses); single snapshot
  ``gpt-6-luna``; ``reasoning.effort`` supports none, low, medium
  (default), high, xhigh and max ("minimal" is not supported).
- https://developers.openai.com/api/docs/pricing?latest-pricing=standard:
  per 1M tokens, input $0.10, cached input $0.01, cache writes $0.125,
  output $0.50.

Prices are USD per 1M tokens, Standard tier (synchronous). Batch
ingestion estimates keep their own documented discount in
``recipes/llm_batch.py``. Re-verify before any live run and bump
``PRICING_VERSION`` when a price changes.
"""

from __future__ import annotations

from dataclasses import dataclass

PRICING_VERSION = "2026-09-24-luna-v1"
DEFAULT_MODEL = "gpt-6-luna"


@dataclass(frozen=True)
class ModelPrices:
    input_per_1m: float
    cached_input_per_1m: float | None
    cache_write_per_1m: float | None
    output_per_1m: float
    tier: str
    source_url: str
    date_verified: str


@dataclass(frozen=True)
class ModelSpec:
    name: str
    reasoning_efforts: tuple[str, ...]
    default_reasoning_effort: str
    prices: ModelPrices
    docs_url: str


SUPPORTED_MODELS: dict[str, ModelSpec] = {
    "gpt-6-luna": ModelSpec(
        name="gpt-6-luna",
        reasoning_efforts=("none", "low", "medium", "high", "xhigh", "max"),
        default_reasoning_effort="medium",
        prices=ModelPrices(
            input_per_1m=0.10,
            cached_input_per_1m=0.01,
            cache_write_per_1m=0.125,
            output_per_1m=0.50,
            tier="standard",
            source_url="https://developers.openai.com/api/docs/pricing?latest-pricing=standard",
            date_verified="2026-09-24",
        ),
        docs_url="https://developers.openai.com/api/docs/models/gpt-6-luna",
    ),
}


def model_spec(name: str | None) -> ModelSpec | None:
    """Registry entry for ``name``, or None for an unsupported model."""
    if not name:
        return None
    return SUPPORTED_MODELS.get(name)


def require_supported_model(name: str) -> str:
    """Return ``name`` if supported; raise ValueError otherwise."""
    if name not in SUPPORTED_MODELS:
        raise ValueError(
            f"Unsupported model {name!r}; supported models: {sorted(SUPPORTED_MODELS)}. "
            "Add a verified entry to culinary_copilot/llm/models.py to support another."
        )
    return name
