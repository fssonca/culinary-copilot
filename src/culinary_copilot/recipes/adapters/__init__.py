"""Source adapters feeding a common normalized recipe model."""

from culinary_copilot.recipes.adapters.base import (
    AdapterResult,
    Provenance,
    fingerprint,
    foodie_source_id,
)
from culinary_copilot.recipes.adapters.foodcom import normalize_foodcom
from culinary_copilot.recipes.adapters.foodie import (
    FOODIE_ADAPTER_VERSION,
    normalize_foodie_text,
)

__all__ = [
    "AdapterResult",
    "FOODIE_ADAPTER_VERSION",
    "Provenance",
    "fingerprint",
    "foodie_source_id",
    "normalize_foodcom",
    "normalize_foodie_text",
]
